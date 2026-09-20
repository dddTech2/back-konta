"""Pruebas de la revisión base de Alembic (Story 1.5): esquema, stamp, downgrade y divergencia
contra Base.metadata. Todo corre sobre SQLite temporal; nunca se toca kontable.db."""

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, MetaData, String, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from dian_automation.db import models  # noqa: F401  (registra las tablas en Base)
from dian_automation.db.database import Base

ROOT = Path(__file__).resolve().parent.parent
BASELINE_TABLES = {
    "users",
    "businesses",
    "invoices",
    "monthly_tax_summaries",
    "dian_extraction_jobs",
    "telegram_link_tokens",
    "subscriptions",
    "payment_records",
    "dian_tax_calendar",
}
EXPECTED_TABLES = BASELINE_TABLES | {"sales", "otp_codes", "worker_heartbeats"}  # esquema en head: base + revisiones posteriores


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{(tmp_path / 'alembic_test.db').as_posix()}"


@pytest.fixture
def alembic_cfg(db_url):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def engine(db_url):
    eng = create_engine(db_url)
    yield eng
    eng.dispose()


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _diff(engine, metadata: MetaData):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return compare_metadata(ctx, metadata)


def _head(cfg: Config) -> str:
    return ScriptDirectory.from_config(cfg).get_current_head()


def _version_rows(engine) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT version_num FROM alembic_version"))]


def _insert_user(engine, user_id: str = "u-1"):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, full_name, role, is_telegram_linked, is_active,"
                " created_at, updated_at) VALUES (:id, 'a@b.co', 'Ana', 'CLIENT', 0, 1,"
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            ),
            {"id": user_id},
        )


def _count_users(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one()


def test_history_is_a_single_chain_rooted_at_the_baseline(alembic_cfg):
    script = ScriptDirectory.from_config(alembic_cfg)
    assert len(script.get_heads()) == 1
    assert script.get_revision("0001").down_revision is None
    assert script.get_revision("0002").down_revision == "0001"
    assert script.get_revision("0003").down_revision == "0002"
    assert script.get_revision("0004").down_revision == "0003"
    assert script.get_revision("0005").down_revision == "0004"
    assert script.get_revision("0006").down_revision == "0005"
    assert script.get_revision("0007").down_revision == "0006"


def test_baseline_revision_creates_only_the_nine_original_tables(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "0001")

    assert _tables(engine) == BASELINE_TABLES | {"alembic_version"}
    assert _version_rows(engine) == ["0001"]


def test_sales_revision_upgrade_and_downgrade_one_step(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "0002")
    assert "sales" in _tables(engine)
    assert {i["name"] for i in inspect(engine).get_indexes("sales")} == {"idx_sales_business_created"}

    command.downgrade(alembic_cfg, "-1")

    assert _tables(engine) == BASELINE_TABLES | {"alembic_version"}
    assert _version_rows(engine) == ["0001"]


def test_otp_codes_revision_upgrade_and_downgrade_one_step(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "0003")
    assert "otp_codes" in _tables(engine)
    assert {i["name"] for i in inspect(engine).get_indexes("otp_codes")} == {"idx_otp_user_created"}

    command.downgrade(alembic_cfg, "-1")

    assert _tables(engine) == BASELINE_TABLES | {"sales", "alembic_version"}
    assert _version_rows(engine) == ["0002"]


def test_worker_heartbeats_revision_upgrade_and_downgrade_one_step(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "0006")
    columns = {c["name"]: c for c in inspect(engine).get_columns("worker_heartbeats")}
    assert set(columns) == {"id", "name", "last_seen_at", "last_alert_at", "created_at"}
    assert columns["last_seen_at"]["nullable"] and columns["last_alert_at"]["nullable"]
    assert not columns["name"]["nullable"]
    uniques = inspect(engine).get_unique_constraints("worker_heartbeats")
    assert [u["column_names"] for u in uniques] == [["name"]]

    command.downgrade(alembic_cfg, "-1")

    assert "worker_heartbeats" not in _tables(engine)
    assert _version_rows(engine) == ["0005"]


def _insert_calendar_row(engine, row_id: str = "c-1", digit: int = 9):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dian_tax_calendar (id, tax_type, fiscal_year, period_label, nit_last_digit,"
                " deadline_date, description, created_at) VALUES (:id, 'IVA BIMESTRAL', 2026,"
                " 'Jul – Ago 2026', :digit, '2026-09-10', 'Formulario 300', '2026-01-01 00:00:00')"
            ),
            {"id": row_id, "digit": digit},
        )


def _calendar_columns(engine) -> dict:
    return {c["name"]: c for c in inspect(engine).get_columns("dian_tax_calendar")}


def test_calendar_ranges_revision_backfills_existing_rows_and_matches_model(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "0003")
    _insert_calendar_row(engine)

    command.upgrade(alembic_cfg, "head")

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT key_length, key_from, key_to, installment, jurisdiction, period_start, period_end,"
                " nit_last_digit FROM dian_tax_calendar"
            )
        ).one()
    assert tuple(row) == (1, 9, 9, 0, "", None, None, 9)
    columns = _calendar_columns(engine)
    assert columns["nit_last_digit"]["nullable"] is True
    for name in ("key_length", "key_from", "key_to", "installment", "jurisdiction"):
        assert columns[name]["nullable"] is False, name
        assert columns[name]["default"] is None, name  # los defaults temporales no quedan en el esquema
    indexes = {i["name"]: i for i in inspect(engine).get_indexes("dian_tax_calendar")}
    assert indexes["uq_dian_tax_calendar_obligation"]["unique"] == 1
    assert indexes["uq_dian_tax_calendar_obligation"]["column_names"] == [
        "tax_type", "fiscal_year", "period_label", "installment", "jurisdiction",
        "key_length", "key_from", "key_to",
    ]
    assert indexes["idx_dian_tax_calendar_lookup"]["column_names"] == [
        "tax_type", "fiscal_year", "key_length", "key_from", "key_to",
    ]
    assert {"ix_dian_tax_calendar_nit_last_digit", "ix_dian_tax_calendar_tax_type",
            "ix_dian_tax_calendar_fiscal_year"} <= set(indexes)
    assert _diff(engine, Base.metadata) == []


def test_calendar_ranges_revision_keeps_one_row_when_legacy_rows_repeat_a_key(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "0003")
    _insert_calendar_row(engine, "a", 9)
    _insert_calendar_row(engine, "b", 9)  # mismo dígito y periodo: la tabla anterior lo permitía
    _insert_calendar_row(engine, "c", 4)

    command.upgrade(alembic_cfg, "head")

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, key_from FROM dian_tax_calendar ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [("a", 9), ("c", 4)]
    assert _version_rows(engine) == [_head(alembic_cfg)]


def test_calendar_row_without_any_key_is_rejected_instead_of_defaulting_to_ending_zero(engine):
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    session.add(models.DIANTaxCalendar(tax_type="X", fiscal_year=2026, period_label="P",
                                       deadline_date=date(2026, 2, 10)))
    with pytest.raises(IntegrityError):
        session.commit()
    session.close()


def test_calendar_ranges_unique_index_rejects_a_repeated_obligation(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "head")
    insert = text(
        "INSERT INTO dian_tax_calendar (id, tax_type, fiscal_year, period_label, key_length, key_from, key_to,"
        " installment, jurisdiction, deadline_date, created_at) VALUES (:id, 'RETEFUENTE', 2026, 'Enero',"
        " 1, 1, 1, 0, '', '2026-02-10', '2026-01-01 00:00:00')"
    )

    with engine.begin() as conn:
        conn.execute(insert, {"id": "a"})
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert, {"id": "b"})


def test_calendar_ranges_downgrade_restores_old_schema_and_keeps_only_single_digit_rows(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "0003")
    _insert_calendar_row(engine, "legacy", 4)
    command.upgrade(alembic_cfg, "0004")
    insert = text(
        "INSERT INTO dian_tax_calendar (id, tax_type, fiscal_year, period_label, key_length, key_from, key_to,"
        " installment, jurisdiction, deadline_date, created_at) VALUES (:id, :tax, 2026, 'P', :kl, :kf, :kt,"
        " :inst, '', '2026-03-10', '2026-01-01 00:00:00')"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"id": "digit", "tax": "IVA", "kl": 1, "kf": 3, "kt": 3, "inst": 0})  # cabe
        conn.execute(insert, {"id": "pair", "tax": "RENTA", "kl": 2, "kf": 1, "kt": 2, "inst": 0})  # no cabe
        conn.execute(insert, {"id": "quota", "tax": "RENTA", "kl": 1, "kf": 5, "kt": 5, "inst": 2})  # no cabe

    command.downgrade(alembic_cfg, "-1")

    assert _version_rows(engine) == ["0003"]
    assert set(_calendar_columns(engine)) == {
        "id", "tax_type", "fiscal_year", "period_label", "nit_last_digit", "deadline_date", "description",
        "created_at",
    }
    assert _calendar_columns(engine)["nit_last_digit"]["nullable"] is False
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, nit_last_digit FROM dian_tax_calendar ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [("digit", 3), ("legacy", 4)]


def test_migrated_sales_table_enforces_positive_total(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "head")
    insert = text(
        "INSERT INTO sales (id, business_id, total_amount, recorded_via, recorded_by_user_id, sale_date, created_at)"
        " VALUES (:id, 'b', :total, 'TELEGRAM', 'u', '2026-01-01', '2026-01-01 00:00:00')"
    )

    with engine.begin() as conn:
        conn.execute(insert, {"id": "ok", "total": "10.00"})
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert, {"id": "zero", "total": "0"})


def test_upgrade_on_empty_db_creates_all_tables_without_divergence(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "head")

    assert _tables(engine) == EXPECTED_TABLES | {"alembic_version"}
    assert _version_rows(engine) == [_head(alembic_cfg)]
    assert _diff(engine, Base.metadata) == []


def test_stamp_on_existing_db_only_adds_version_table(alembic_cfg, engine):
    Base.metadata.create_all(bind=engine)
    _insert_user(engine)

    command.stamp(alembic_cfg, "head")

    assert _tables(engine) == EXPECTED_TABLES | {"alembic_version"}
    assert _version_rows(engine) == [_head(alembic_cfg)]
    assert _count_users(engine) == 1

    command.upgrade(alembic_cfg, "head")  # ya en head: no cambia nada

    assert _tables(engine) == EXPECTED_TABLES | {"alembic_version"}
    assert _count_users(engine) == 1
    assert _diff(engine, Base.metadata) == []


def test_upgrade_without_stamp_fails_and_leaves_data_intact(alembic_cfg, engine):
    Base.metadata.create_all(bind=engine)
    _insert_user(engine)

    with pytest.raises(OperationalError, match="already exists"):
        command.upgrade(alembic_cfg, "head")

    assert _count_users(engine) == 1
    assert _tables(engine) - {"alembic_version"} == EXPECTED_TABLES


def test_downgrade_base_drops_all_tables(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "head")

    command.downgrade(alembic_cfg, "base")

    assert _tables(engine) == {"alembic_version"}
    assert _version_rows(engine) == []


def test_divergence_detects_model_column_without_revision(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "head")
    assert _diff(engine, Base.metadata) == []

    diverged = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(diverged)
    diverged.tables["users"].append_column(Column("nickname", String(50)))

    diff = _diff(engine, diverged)

    assert [(op[0], op[3].name) for op in diff] == [("add_column", "nickname")]


def test_bot_and_worker_runners_no_longer_call_init_db():
    for runner in ("run_telegram_bot.py", "run_worker.py"):
        assert "init_db" not in (ROOT / runner).read_text(encoding="utf-8"), runner
    assert "def init_db" in (ROOT / "src/dian_automation/db/database.py").read_text(encoding="utf-8")


def test_bot_startup_without_migrating_does_not_create_tables(engine, monkeypatch):
    import run_telegram_bot
    from dian_automation.db import database

    # init_db() (si volviera a llamarse) usa database.engine: apuntarlo a la base temporal.
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(run_telegram_bot.TelegramBotRunner, "verify_token", lambda self: False)

    run_telegram_bot.TelegramBotRunner(bot_token="fake-token").run()

    assert _tables(engine) == set()


def test_worker_startup_without_migrating_does_not_create_tables(engine, monkeypatch):
    import run_worker
    from dian_automation.db import database

    class _NoLoopWorker:
        def __init__(self, db_session_factory):
            pass

        def run_loop(self, **kwargs):
            return None

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(run_worker, "ExtractionWorker", _NoLoopWorker)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_TECH_OPS_BOT_TOKEN", raising=False)

    run_worker.main()

    assert _tables(engine) == set()


def test_cli_uses_database_url_from_environment(tmp_path):
    db_file = tmp_path / "cli_test.db"
    env_url = f"sqlite:///{db_file.as_posix()}"
    env = {**os.environ, "DATABASE_URL": env_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    cli_engine = create_engine(env_url)
    try:
        assert _tables(cli_engine) == EXPECTED_TABLES | {"alembic_version"}
    finally:
        cli_engine.dispose()


def test_default_database_url_when_env_absent():
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from dian_automation.db.database import DATABASE_URL; print(DATABASE_URL)",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sqlite:///./kontable.db"


def _insert_business(engine, business_id: str = "b-1"):
    _insert_user(engine, f"u-{business_id}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO businesses (id, client_id, legal_name, commercial_name, nit, dv, taxpayer_type,"
                " is_active, created_at, updated_at) VALUES (:id, :client, 'Ana', 'Ana', '901008579', '7',"
                " 'PERSONA_NATURAL', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            ),
            {"id": business_id, "client": f"u-{business_id}"},
        )


def _business_columns(engine) -> dict:
    return {c["name"]: c for c in inspect(engine).get_columns("businesses")}


def test_business_income_source_revision_defaults_existing_rows_and_matches_model(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "0004")
    _insert_business(engine)

    command.upgrade(alembic_cfg, "head")

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT income_source, iva_periodicity, is_withholding_agent FROM businesses")
        ).one()
    assert tuple(row) == ("DIAN", None, 0)
    columns = _business_columns(engine)
    assert columns["income_source"]["nullable"] is False
    assert columns["is_withholding_agent"]["nullable"] is False
    assert columns["iva_periodicity"]["nullable"] is True
    assert columns["income_source"]["type"].length == 20 and columns["iva_periodicity"]["type"].length == 20
    assert _diff(engine, Base.metadata) == []


def test_business_income_source_revision_downgrade_drops_the_columns_and_keeps_rows(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "0005")
    _insert_business(engine)

    command.downgrade(alembic_cfg, "-1")

    assert _version_rows(engine) == ["0004"]
    assert {"income_source", "iva_periodicity", "is_withholding_agent"}.isdisjoint(_business_columns(engine))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM businesses")).scalar_one() == 1


def test_sales_sale_date_revision_backfills_and_downgrades(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "0006")
    _insert_business(engine, "biz-mig")

    # Venta insertada bajo el esquema previo (sin sale_date)
    # 2026-09-21 04:30:00 UTC corresponde a las 23:30 del 2026-09-20 en América/Bogotá (UTC-5)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sales (id, business_id, total_amount, recorded_via, recorded_by_user_id, created_at)"
                " VALUES ('sale-mig', 'biz-mig', 50000.00, 'TELEGRAM', 'u-biz-mig', '2026-09-21 04:30:00')"
            )
        )

    command.upgrade(alembic_cfg, "0007")

    with engine.connect() as conn:
        row = conn.execute(text("SELECT sale_date FROM sales WHERE id = 'sale-mig'")).one()
    assert str(row[0]) == "2026-09-20"

    columns = {c["name"]: c for c in inspect(engine).get_columns("sales")}
    assert "sale_date" in columns
    assert columns["sale_date"]["nullable"] is False

    indexes = {i["name"]: i for i in inspect(engine).get_indexes("sales")}
    assert "idx_sales_business_sale_date" in indexes
    assert indexes["idx_sales_business_sale_date"]["column_names"] == ["business_id", "sale_date"]

    assert _diff(engine, Base.metadata) == []

    command.downgrade(alembic_cfg, "-1")

    assert _version_rows(engine) == ["0006"]
    columns_after = {c["name"]: c for c in inspect(engine).get_columns("sales")}
    assert "sale_date" not in columns_after
    indexes_after = {i["name"]: i for i in inspect(engine).get_indexes("sales")}
    assert "idx_sales_business_sale_date" not in indexes_after

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM sales")).scalar_one() == 1
