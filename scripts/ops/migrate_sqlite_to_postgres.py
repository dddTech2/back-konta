"""Script de migración de datos desde SQLite hacia PostgreSQL para Konta.

Modo de uso para trasladar una base de datos existente:
1) Ejecutar 'alembic upgrade head' sobre un PostgreSQL vacío para crear el esquema en head.
2) Asegurar que la base de datos SQLite origen esté en la revisión 'head' de Alembic.
3) Detener los servicios en producción (api, bot, scheduler).
4) Ejecutar este script apuntando al archivo SQLite y a la URL de PostgreSQL:
     uv run python scripts/ops/migrate_sqlite_to_postgres.py --sqlite ./kontable.db --postgres postgresql+psycopg://usuario:clave@host:5432/kontable
5) Arrancar los servicios apuntando a PostgreSQL.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url

from dian_automation.db import models  # noqa: F401 (registra los modelos en Base.metadata)
from dian_automation.db.database import Base, normalize_database_url


class MigrationAborted(Exception):
    """Excepción lanzada cuando la migración se aborta por validación o error."""
    pass


def _get_alembic_revision(engine: Engine) -> Optional[str]:
    """Obtiene la revisión actual en la tabla alembic_version, o None si no existe o está vacía."""
    with engine.connect() as conn:
        tables = inspect(conn).get_table_names()
        if "alembic_version" not in tables:
            return None
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        return str(row[0]) if row and row[0] is not None else None


def copy_database(
    source_engine: Engine,
    target_engine: Engine,
    *,
    truncate: bool = False,
    chunk_size: int = 1000,
) -> Dict[str, int]:
    """Copia los datos de todas las tablas del modelo desde source_engine a target_engine.

    Precondiciones:
      (a) Origen y destino tienen la tabla 'alembic_version' y la misma revisión.
      (b) El destino no tiene filas en ninguna tabla del modelo, salvo que truncate=True,
          en cuyo caso se vacían en orden inverso de dependencias antes de copiar.

    Toda la copia en destino ocurre dentro de una sola transacción (target_engine.begin()).
    Al finalizar la copia y dentro de la misma transacción, compara conteos por tabla.
    Si algún conteo difiere, lanza MigrationAborted y revierte la transacción.

    Devuelve un diccionario con el conteo de filas copiadas por cada tabla.
    """
    source_rev = _get_alembic_revision(source_engine)
    target_rev = _get_alembic_revision(target_engine)

    if target_rev is None:
        raise MigrationAborted(
            "La tabla 'alembic_version' no existe en la base destino. "
            "Aplica 'alembic upgrade head' en la base destino antes de migrar datos."
        )

    if source_rev is None:
        raise MigrationAborted(
            "La tabla 'alembic_version' no existe en la base origen. "
            "Aplica 'alembic upgrade head' en la base origen antes de migrar datos."
        )

    if source_rev != target_rev:
        raise MigrationAborted(
            f"Las revisiones de esquema difieren (origen: '{source_rev}', destino: '{target_rev}'). "
            "Aplica 'alembic upgrade head' en cada base antes de migrar datos."
        )

    # Validar si el destino tiene filas previas cuando truncate=False
    if not truncate:
        target_counts: Dict[str, int] = {}
        with target_engine.connect() as conn:
            target_tables = set(inspect(conn).get_table_names())
            for table in Base.metadata.sorted_tables:
                if table.name in target_tables:
                    count = conn.execute(select(func.count()).select_from(table)).scalar_one()
                    if count > 0:
                        target_counts[table.name] = count

        if target_counts:
            detalles = ", ".join(f"{t}: {c}" for t, c in sorted(target_counts.items()))
            raise MigrationAborted(
                f"El destino contiene datos en las tablas: {detalles}. "
                "Usa --truncate para vaciarlas o migra hacia una base limpia."
            )

    counts: Dict[str, int] = {}

    with target_engine.begin() as target_conn:
        if truncate:
            for table in reversed(Base.metadata.sorted_tables):
                target_conn.execute(table.delete())

        with source_engine.connect() as source_conn:
            for table in Base.metadata.sorted_tables:
                result = source_conn.execute(select(table))
                copied_for_table = 0

                while True:
                    rows = result.fetchmany(chunk_size)
                    if not rows:
                        break
                    batch = [dict(row._mapping) for row in rows]
                    target_conn.execute(table.insert(), batch)
                    copied_for_table += len(batch)

                # Comparación de conteos dentro de la misma transacción en destino
                source_count = source_conn.execute(select(func.count()).select_from(table)).scalar_one()
                target_count = target_conn.execute(select(func.count()).select_from(table)).scalar_one()

                if source_count != target_count:
                    raise MigrationAborted(
                        f"Discrepancia de conteo en la tabla '{table.name}': "
                        f"el origen tiene {source_count} filas pero el destino tiene {target_count}."
                    )

                counts[table.name] = target_count

    return counts


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Punto de entrada de línea de comandos."""
    parser = argparse.ArgumentParser(
        description="Traslada datos de Konta desde SQLite hacia PostgreSQL."
    )
    parser.add_argument(
        "--sqlite",
        required=True,
        help="Ruta de archivo o URL SQLite (ej. ./kontable.db o sqlite:///...)",
    )
    parser.add_argument(
        "--postgres",
        default=None,
        help="URL de conexión a PostgreSQL (por defecto toma DATABASE_URL del entorno)",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Vacía las tablas del destino en orden de dependencias antes de copiar",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1000,
        help="Número de filas leídas e insertadas por lote (por defecto 1000)",
    )

    args = parser.parse_args(argv)

    # Procesar URL de origen SQLite
    if args.sqlite.startswith("sqlite://"):
        sqlite_url = args.sqlite
    else:
        sqlite_path = Path(args.sqlite).resolve()
        sqlite_url = f"sqlite:///{sqlite_path.as_posix()}"

    # Procesar URL de destino PostgreSQL
    pg_raw = args.postgres or os.getenv("DATABASE_URL")
    if not pg_raw:
        print(
            "Error: Se requiere una URL de PostgreSQL mediante --postgres o la variable DATABASE_URL.",
            file=sys.stderr,
        )
        return 1

    postgres_url = normalize_database_url(pg_raw)
    if not (postgres_url.startswith("postgresql://") or postgres_url.startswith("postgresql+psycopg://")):
        print(
            "Error: La URL de destino debe ser una conexión a PostgreSQL.",
            file=sys.stderr,
        )
        return 1

    sanitized_target = make_url(postgres_url).render_as_string(hide_password=True)
    print(f"Destino: {sanitized_target}")

    # hide_parameters: si una inserción falla, SQLAlchemy no imprime los valores de la fila (datos de clientes)
    source_engine = create_engine(sqlite_url, connect_args={"check_same_thread": False}, hide_parameters=True)
    target_engine = create_engine(postgres_url, hide_parameters=True)

    try:
        counts = copy_database(
            source_engine,
            target_engine,
            truncate=args.truncate,
            chunk_size=args.chunk_size,
        )
        print("Conteo de filas copiadas por tabla:")
        total_rows = 0
        for table_name, count in counts.items():
            print(f"  {table_name}: {count}")
            total_rows += count
        print(f"Migración finalizada con éxito. Total filas copiadas: {total_rows}")
        return 0
    except MigrationAborted as err:
        print(f"Migración cancelada: {err}", file=sys.stderr)
        return 1
    except Exception as err:
        print(f"Error inesperado durante la migración: {err}", file=sys.stderr)
        return 1
    finally:
        source_engine.dispose()
        target_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
