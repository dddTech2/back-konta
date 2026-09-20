"""Fixtures compartidas: secreto JWT de prueba y emisión de Bearer por usuario."""

import os
from types import SimpleNamespace
import uuid

import pytest
import sqlalchemy
import sqlalchemy.engine
from sqlalchemy.pool import NullPool

from dian_automation.core import auth_service

# --- Modo PostgreSQL opcional -------------------------------------------------------------------
# Con TEST_DATABASE_URL=postgresql+psycopg://usuario:clave@host:puerto/base la suite completa corre
# contra PostgreSQL: cada `create_engine("sqlite://...")` en memoria de las pruebas se redirige a un
# esquema propio y desechable de esa base (search_path), que se borra al terminar la sesión. Los
# engines a archivos SQLite (pruebas de Alembic) no se tocan. Sin la variable no cambia nada.
_PG_TEST_URL = os.getenv("TEST_DATABASE_URL", "")
_pg_schemas: list = []
_pg_admin = None

if _PG_TEST_URL.startswith("postgresql"):
    _original_create_engine = sqlalchemy.create_engine

    def _pg_admin_engine():
        global _pg_admin
        if _pg_admin is None:
            _pg_admin = _original_create_engine(_PG_TEST_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        return _pg_admin

    def _create_engine_for_tests(url, *args, **kwargs):
        text_url = str(url)
        if text_url.startswith("sqlite") and (":memory:" in text_url or text_url in ("sqlite://", "sqlite:///")):
            schema = "t_" + uuid.uuid4().hex[:12]
            with _pg_admin_engine().connect() as conn:
                conn.execute(sqlalchemy.text(f'CREATE SCHEMA "{schema}"'))
            _pg_schemas.append(schema)
            return _original_create_engine(
                _PG_TEST_URL, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
            )
        return _original_create_engine(url, *args, **kwargs)

    sqlalchemy.create_engine = _create_engine_for_tests
    sqlalchemy.engine.create_engine = _create_engine_for_tests


def pytest_sessionfinish(session, exitstatus):
    if _pg_schemas:
        with _pg_admin_engine().connect() as conn:
            for schema in _pg_schemas:
                conn.execute(sqlalchemy.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

TEST_JWT_SECRET = "secreto-jwt-solo-para-pruebas-0123456789"


@pytest.fixture(autouse=True)
def jwt_test_config(monkeypatch):
    """Fija la configuración JWT de AuthService sin depender del .env ni del entorno."""
    fake = SimpleNamespace(jwt_secret=TEST_JWT_SECRET, jwt_algorithm="HS256", jwt_ttl_minutes=60)
    monkeypatch.setattr(auth_service, "config", fake)
    return fake


@pytest.fixture
def bearer():
    """Devuelve `bearer(user_id) -> {"Authorization": "Bearer <jwt>"}` firmado con el secreto de prueba."""

    def _make(user_id: str) -> dict:
        token = auth_service.create_access_token(SimpleNamespace(id=user_id))
        return {"Authorization": f"Bearer {token}"}

    return _make
