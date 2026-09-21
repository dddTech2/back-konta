"""Pruebas para el script de traslado de SQLite a PostgreSQL (scripts/ops/migrate_sqlite_to_postgres.py)."""

import importlib.util
import os
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from dian_automation.db import models  # noqa: F401
from dian_automation.db.database import Base

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "ops" / "migrate_sqlite_to_postgres.py"

# Carga dinámica del script a probar
spec = importlib.util.spec_from_file_location("migrate_sqlite_to_postgres", SCRIPT_PATH)
migrate_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate_module)

copy_database = migrate_module.copy_database
MigrationAborted = migrate_module.MigrationAborted


def _setup_sqlite_db(engine, revision: str = "0008"):
    """Crea todas las tablas del modelo y una tabla alembic_version con la revisión dada."""
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text(f"INSERT INTO alembic_version (version_num) VALUES ('{revision}')"))


def _seed_source_orm(engine):
    """Siembra filas coherentes en las tablas principales respetando llaves foráneas."""
    Session = sessionmaker(bind=engine)
    with Session() as session:
        user = models.User(
            id="u-1",
            email="ana@empresa.co",
            full_name="Ana Gomez",
            role="CLIENT",
            is_telegram_linked=False,
            is_active=True,
            created_at=datetime(2026, 1, 1, 0, 0, 0),
            updated_at=datetime(2026, 1, 1, 0, 0, 0),
        )
        biz = models.Business(
            id="b-1",
            client_id="u-1",
            legal_name="Ana Gomez SAS",
            commercial_name="AG SAS",
            nit="900123456",
            dv="7",
            taxpayer_type="PERSONA_NATURAL",
            income_source="DIAN",
            is_withholding_agent=False,
            is_active=True,
            created_at=datetime(2026, 1, 1, 0, 0, 0),
            updated_at=datetime(2026, 1, 1, 0, 0, 0),
        )
        inv = models.Invoice(
            id="i-1",
            business_id="b-1",
            document_type="Factura electrónica",
            cufe="cufe-xyz-123",
            currency="COP",
            issue_date=datetime(2026, 9, 30, 23, 59, 59),
            issuer_nit="900123456",
            issuer_name="Ana Gomez SAS",
            receiver_nit="800987654",
            receiver_name="Cliente Corp",
            iva=Decimal("19000.50"),
            ica=Decimal("0.00"),
            inc=Decimal("0.00"),
            timbre=Decimal("0.00"),
            inc_bolsas=Decimal("0.00"),
            in_carbono=Decimal("0.00"),
            in_combustibles=Decimal("0.00"),
            ibua=Decimal("0.00"),
            icui=Decimal("0.00"),
            rete_iva=Decimal("0.00"),
            rete_renta=Decimal("0.00"),
            rete_ica=Decimal("0.00"),
            total=Decimal("119000.50"),
            group_type="Emitido",
            created_at=datetime(2026, 9, 30, 23, 59, 59),
        )
        sale = models.Sale(
            id="s-1",
            business_id="b-1",
            total_amount=Decimal("54321.80"),
            recorded_via="TELEGRAM",
            recorded_by_user_id="u-1",
            sale_date=date(2026, 9, 25),
            voided_at=datetime(2026, 9, 25, 14, 0, 0),
            voided_by_user_id="u-1",
            created_at=datetime(2026, 9, 25, 12, 0, 0),
        )
        job = models.DIANExtractionJob(
            id="j-1",
            business_id="b-1",
            target_period="2026-09",
            status="SUCCESS",
            attempt_count=1,
            max_attempts=3,
            next_run_at=datetime(2026, 9, 20, 0, 0, 0),
            created_at=datetime(2026, 9, 20, 0, 0, 0),
        )
        session.add_all([user, biz, inv, sale, job])
        session.commit()


def test_copy_database_success(tmp_path):
    """Verifica que copy_database copia filas y preserva exactamente valores, fechas y Decimales."""
    src_path = tmp_path / "src.db"
    dst_path = tmp_path / "dst.db"

    src_engine = create_engine(f"sqlite:///{src_path.as_posix()}", connect_args={"check_same_thread": False})
    dst_engine = create_engine(f"sqlite:///{dst_path.as_posix()}", connect_args={"check_same_thread": False})

    try:
        _setup_sqlite_db(src_engine)
        _setup_sqlite_db(dst_engine)
        _seed_source_orm(src_engine)

        counts = copy_database(src_engine, dst_engine)

        assert counts["users"] == 1
        assert counts["businesses"] == 1
        assert counts["invoices"] == 1
        assert counts["sales"] == 1
        assert counts["dian_extraction_jobs"] == 1

        Session = sessionmaker(bind=dst_engine)
        with Session() as session:
            inv = session.query(models.Invoice).filter_by(id="i-1").one()
            assert inv.total == Decimal("119000.50")
            assert inv.iva == Decimal("19000.50")
            assert inv.issue_date == datetime(2026, 9, 30, 23, 59, 59)
            assert inv.business_id == "b-1"

            sale = session.query(models.Sale).filter_by(id="s-1").one()
            assert sale.total_amount == Decimal("54321.80")
            assert sale.voided_by_user_id == "u-1"
            assert sale.sale_date == date(2026, 9, 25)

            job = session.query(models.DIANExtractionJob).filter_by(id="j-1").one()
            assert job.target_period == "2026-09"
            assert job.status == "SUCCESS"
    finally:
        src_engine.dispose()
        dst_engine.dispose()


def test_copy_database_rejects_non_empty_target_without_truncate(tmp_path):
    """Rechaza la migración si el destino ya contiene filas y truncate=False, sin modificar el destino."""
    src_path = tmp_path / "src.db"
    dst_path = tmp_path / "dst.db"

    src_engine = create_engine(f"sqlite:///{src_path.as_posix()}", connect_args={"check_same_thread": False})
    dst_engine = create_engine(f"sqlite:///{dst_path.as_posix()}", connect_args={"check_same_thread": False})

    try:
        _setup_sqlite_db(src_engine)
        _setup_sqlite_db(dst_engine)
        _seed_source_orm(src_engine)

        # Sembrar fila previa en el destino
        Session = sessionmaker(bind=dst_engine)
        with Session() as session:
            session.add(
                models.User(
                    id="u-prev",
                    email="prev@empresa.co",
                    full_name="Previo",
                    role="CLIENT",
                    is_telegram_linked=False,
                    is_active=True,
                    created_at=datetime(2026, 1, 1, 0, 0, 0),
                    updated_at=datetime(2026, 1, 1, 0, 0, 0),
                )
            )
            session.commit()

        with pytest.raises(MigrationAborted) as exc_info:
            copy_database(src_engine, dst_engine, truncate=False)

        error_msg = str(exc_info.value)
        assert "users: 1" in error_msg
        # Garantizar que no se imprimen datos de filas, solo conteo y nombre de tabla
        assert "prev@empresa.co" not in error_msg

        # El destino no fue alterado
        with Session() as session:
            users = session.query(models.User).all()
            assert len(users) == 1
            assert users[0].id == "u-prev"
            assert session.query(models.Business).count() == 0
    finally:
        src_engine.dispose()
        dst_engine.dispose()


def test_copy_database_replaces_target_content_when_truncate_true(tmp_path):
    """Con truncate=True reemplaza los datos preexistentes en el destino respetando dependencias."""
    src_path = tmp_path / "src.db"
    dst_path = tmp_path / "dst.db"

    src_engine = create_engine(f"sqlite:///{src_path.as_posix()}", connect_args={"check_same_thread": False})
    dst_engine = create_engine(f"sqlite:///{dst_path.as_posix()}", connect_args={"check_same_thread": False})

    try:
        _setup_sqlite_db(src_engine)
        _setup_sqlite_db(dst_engine)
        _seed_source_orm(src_engine)

        Session = sessionmaker(bind=dst_engine)
        with Session() as session:
            session.add(
                models.User(
                    id="u-old",
                    email="old@empresa.co",
                    full_name="Old",
                    role="CLIENT",
                    is_telegram_linked=False,
                    is_active=True,
                    created_at=datetime(2026, 1, 1, 0, 0, 0),
                    updated_at=datetime(2026, 1, 1, 0, 0, 0),
                )
            )
            session.commit()

        counts = copy_database(src_engine, dst_engine, truncate=True)
        assert counts["users"] == 1

        with Session() as session:
            assert session.query(models.User).filter_by(id="u-old").first() is None
            assert session.query(models.User).filter_by(id="u-1").first() is not None
            assert session.query(models.Business).filter_by(id="b-1").first() is not None
    finally:
        src_engine.dispose()
        dst_engine.dispose()


def test_copy_database_rejects_missing_or_mismatched_alembic_version(tmp_path):
    """Rechaza cuando falta alembic_version en alguna base o cuando las revisiones difieren."""
    src_path = tmp_path / "src.db"
    dst_path = tmp_path / "dst.db"

    src_engine = create_engine(f"sqlite:///{src_path.as_posix()}", connect_args={"check_same_thread": False})
    dst_engine = create_engine(f"sqlite:///{dst_path.as_posix()}", connect_args={"check_same_thread": False})

    try:
        # Caso 1: Destino no tiene la tabla alembic_version
        _setup_sqlite_db(src_engine, revision="0008")
        Base.metadata.create_all(bind=dst_engine)  # sin alembic_version

        with pytest.raises(MigrationAborted) as exc_info1:
            copy_database(src_engine, dst_engine)
        assert "alembic_version" in str(exc_info1.value)

        # Caso 2: Revisiones difieren
        with dst_engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            conn.execute(text("INSERT INTO alembic_version VALUES ('0007')"))

        with pytest.raises(MigrationAborted) as exc_info2:
            copy_database(src_engine, dst_engine)
        assert "difieren" in str(exc_info2.value)
    finally:
        src_engine.dispose()
        dst_engine.dispose()


def test_copy_database_rolls_back_on_midway_failure(tmp_path):
    """Un fallo durante la inserción en el destino revierte toda la transacción sin dejar filas."""
    src_path = tmp_path / "src.db"
    dst_path = tmp_path / "dst.db"

    src_engine = create_engine(f"sqlite:///{src_path.as_posix()}", connect_args={"check_same_thread": False})
    dst_engine = create_engine(f"sqlite:///{dst_path.as_posix()}", connect_args={"check_same_thread": False})

    try:
        _setup_sqlite_db(src_engine)
        _setup_sqlite_db(dst_engine)

        # En el destino creamos una restricción única adicional en phone
        with dst_engine.begin() as conn:
            conn.execute(text("CREATE UNIQUE INDEX idx_test_phone_uniq ON users (phone)"))

        # En el origen insertamos dos usuarios con el mismo phone
        Session = sessionmaker(bind=src_engine)
        with Session() as session:
            u1 = models.User(
                id="u-a",
                email="a@test.co",
                full_name="User A",
                phone="3009998877",
                role="CLIENT",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            u2 = models.User(
                id="u-b",
                email="b@test.co",
                full_name="User B",
                phone="3009998877",  # Choca con el índice único de destino
                role="CLIENT",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add_all([u1, u2])
            session.commit()

        with pytest.raises((IntegrityError, Exception)):
            copy_database(src_engine, dst_engine)

        # El destino debe haber revertido la transacción completa: cero usuarios en destino
        with dst_engine.connect() as conn:
            user_count = conn.execute(select(func.count()).select_from(models.User.__table__)).scalar_one()
            assert user_count == 0
    finally:
        src_engine.dispose()
        dst_engine.dispose()


_PG_TEST_URL = os.getenv("TEST_DATABASE_URL", "")


@pytest.mark.skipif(
    not _PG_TEST_URL.startswith("postgresql"),
    reason="Requiere TEST_DATABASE_URL con PostgreSQL para ejecutar",
)
def test_migrate_to_real_postgres(tmp_path):
    """Prueba de integración de migración real desde SQLite hacia PostgreSQL temporal."""
    admin_engine = create_engine(_PG_TEST_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    db_name = f"kt_mig_{uuid.uuid4().hex[:10]}"

    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))

    target_url = str(make_url(_PG_TEST_URL).set(database=db_name))
    target_engine = create_engine(target_url, poolclass=NullPool)

    sqlite_path = tmp_path / "pg_source.db"
    sqlite_url = f"sqlite:///{sqlite_path.as_posix()}"
    src_engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})

    try:
        # Migrar ambas bases a head con Alembic
        cfg_pg = Config(str(ROOT / "alembic.ini"))
        cfg_pg.set_main_option("sqlalchemy.url", target_url)
        command.upgrade(cfg_pg, "head")

        cfg_sqlite = Config(str(ROOT / "alembic.ini"))
        cfg_sqlite.set_main_option("sqlalchemy.url", sqlite_url)
        command.upgrade(cfg_sqlite, "head")

        _seed_source_orm(src_engine)

        counts = copy_database(src_engine, target_engine)

        assert counts["users"] == 1
        assert counts["businesses"] == 1
        assert counts["invoices"] == 1
        assert counts["sales"] == 1
        assert counts["dian_extraction_jobs"] == 1

        Session = sessionmaker(bind=target_engine)
        with Session() as session:
            inv = session.query(models.Invoice).filter_by(id="i-1").one()
            assert inv.total == Decimal("119000.50")
            assert inv.issue_date == datetime(2026, 9, 30, 23, 59, 59)

            sale = session.query(models.Sale).filter_by(id="s-1").one()
            assert sale.total_amount == Decimal("54321.80")
            assert sale.voided_by_user_id == "u-1"
    finally:
        src_engine.dispose()
        target_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        admin_engine.dispose()
