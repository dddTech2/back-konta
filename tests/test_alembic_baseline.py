"""Pruebas de la revisión base de Alembic (Story 1.5): esquema, stamp, downgrade y divergencia
contra Base.metadata. Todo corre sobre SQLite temporal; nunca se toca kontable.db."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, MetaData, String, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

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
EXPECTED_TABLES = BASELINE_TABLES | {"sales"}  # esquema en head: base + revisiones posteriores


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


def test_baseline_revision_creates_only_the_nine_original_tables(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "0001")

    assert _tables(engine) == BASELINE_TABLES | {"alembic_version"}
    assert _version_rows(engine) == ["0001"]


def test_sales_revision_upgrade_and_downgrade_one_step(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "head")
    assert "sales" in _tables(engine)
    assert {i["name"] for i in inspect(engine).get_indexes("sales")} == {"idx_sales_business_created"}

    command.downgrade(alembic_cfg, "-1")

    assert _tables(engine) == BASELINE_TABLES | {"alembic_version"}
    assert _version_rows(engine) == ["0001"]


def test_migrated_sales_table_enforces_positive_total(alembic_cfg, engine):
    command.upgrade(alembic_cfg, "head")
    insert = text(
        "INSERT INTO sales (id, business_id, total_amount, recorded_via, recorded_by_user_id, created_at)"
        " VALUES (:id, 'b', :total, 'TELEGRAM', 'u', '2026-01-01 00:00:00')"
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
