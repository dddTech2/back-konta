"""Pruebas de integración contra PostgreSQL real (requiere TEST_DATABASE_URL)."""

import os
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from dian_automation.db import models  # noqa: F401
from dian_automation.db.database import Base

ROOT = Path(__file__).resolve().parent.parent

_PG_TEST_URL = os.getenv("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not _PG_TEST_URL.startswith("postgresql"),
    reason="Requiere TEST_DATABASE_URL con PostgreSQL para ejecutar",
)


@pytest.fixture
def postgres_db_url():
    """Crea una base de datos temporal en PostgreSQL para la prueba y la destruye al finalizar."""
    admin_engine = create_engine(_PG_TEST_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    db_name = f"kt_it_{uuid.uuid4().hex[:10]}"

    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))

    target_url = str(make_url(_PG_TEST_URL).set(database=db_name))

    try:
        yield target_url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        admin_engine.dispose()


def test_alembic_upgrade_and_downgrade_cycle(postgres_db_url):
    """Verifica que upgrade head y downgrade base se ejecuten sin errores en PostgreSQL y limpien el esquema."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", postgres_db_url)

    command.upgrade(cfg, "head")

    engine = create_engine(postgres_db_url, poolclass=NullPool)
    try:
        tables_after_upgrade = inspect(engine).get_table_names()
        assert "users" in tables_after_upgrade
        assert "alembic_version" in tables_after_upgrade

        command.downgrade(cfg, "base")

        tables_after_downgrade = inspect(engine).get_table_names()
        assert tables_after_downgrade == ["alembic_version"]
    finally:
        engine.dispose()


def test_migrated_schema_matches_models(postgres_db_url):
    """Verifica que el esquema migrado a head en PostgreSQL contenga exactamente las columnas y nulabilidades del modelo."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", postgres_db_url)

    command.upgrade(cfg, "head")

    engine = create_engine(postgres_db_url, poolclass=NullPool)
    try:
        insp = inspect(engine)
        db_tables = set(insp.get_table_names())
        model_tables = {table.name for table in Base.metadata.sorted_tables}

        # No deben sobrar tablas salvo alembic_version
        assert db_tables - {"alembic_version"} == model_tables

        for table in Base.metadata.sorted_tables:
            cols_in_db = {col["name"]: col for col in insp.get_columns(table.name)}
            cols_in_model = {col.name: col for col in table.columns}

            assert set(cols_in_db.keys()) == set(
                cols_in_model.keys()
            ), f"Discrepancia en columnas para la tabla {table.name}"

            for col_name, model_col in cols_in_model.items():
                db_col = cols_in_db[col_name]
                assert db_col["nullable"] == model_col.nullable, (
                    f"Nulabilidad no coincide para {table.name}.{col_name}: "
                    f"BD={db_col['nullable']}, modelo={model_col.nullable}"
                )
    finally:
        engine.dispose()


def test_decimal_and_datetime_round_trip(postgres_db_url):
    """Inserta y recupera valores Decimal y datetime exactos, y comprueba filtros por rango temporal en PostgreSQL."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", postgres_db_url)

    command.upgrade(cfg, "head")

    engine = create_engine(postgres_db_url, poolclass=NullPool)
    Session = sessionmaker(bind=engine)
    try:
        with Session() as session:
            user = models.User(
                id="u-rt",
                email="roundtrip@test.co",
                full_name="Usuario Roundtrip",
                role="CLIENT",
                is_telegram_linked=False,
                is_active=True,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            biz = models.Business(
                id="b-rt",
                client_id="u-rt",
                legal_name="RT SAS",
                commercial_name="RT",
                nit="900555666",
                dv="3",
                taxpayer_type="PERSONA_NATURAL",
                income_source="DIAN",
                is_withholding_agent=False,
                is_active=True,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            inv = models.Invoice(
                id="i-rt",
                business_id="b-rt",
                document_type="Factura electrónica",
                cufe="cufe-roundtrip-test",
                currency="COP",
                issue_date=datetime(2026, 9, 30, 23, 59, 59),
                issuer_nit="900555666",
                issuer_name="RT SAS",
                receiver_nit="800111222",
                receiver_name="Cliente Destino",
                iva=Decimal("0.00"),
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
                total=Decimal("1234567.89"),
                group_type="Emitido",
                created_at=datetime.utcnow(),
            )
            session.add_all([user, biz, inv])
            session.commit()

        with Session() as session:
            inv_read = session.query(models.Invoice).filter_by(id="i-rt").one()
            assert inv_read.total == Decimal("1234567.89")
            assert inv_read.issue_date == datetime(2026, 9, 30, 23, 59, 59)

            # Filtro por rango temporal: septiembre 2026
            filtered = (
                session.query(models.Invoice)
                .filter(
                    models.Invoice.issue_date >= datetime(2026, 9, 1),
                    models.Invoice.issue_date < datetime(2026, 10, 1),
                )
                .all()
            )
            assert len(filtered) == 1
            assert filtered[0].id == "i-rt"
    finally:
        engine.dispose()
