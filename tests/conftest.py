"""Fixtures compartidas: secreto JWT de prueba y emisión de Bearer por usuario."""

from types import SimpleNamespace

import pytest

from dian_automation.core import auth_service

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
