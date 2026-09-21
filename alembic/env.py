"""Entorno de Alembic para Konta.

Usa la misma DATABASE_URL que la aplicación (dian_automation.db.database) y
Base.metadata como fuente del esquema.
"""

from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

# database.py lee DATABASE_URL al importarse: cargar .env antes.
load_dotenv()

from dian_automation.db import models  # noqa: E402,F401  (registra las tablas en Base)
from dian_automation.db.database import DATABASE_URL, Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _get_url() -> str:
    # Un sqlalchemy.url fijado por programa (p. ej. tests) tiene prioridad;
    # alembic.ini no define ninguno.
    return config.get_main_option("sqlalchemy.url") or DATABASE_URL


def run_migrations_offline() -> None:
    """Genera el SQL sin conectarse a la base."""
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Ejecuta las migraciones contra la base."""
    connectable = create_engine(_get_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
