"""Pruebas de `POST /api/sales/{business_id}` (Story 5.3): adaptador HTTP sobre `core/sales_service.py`."""

import inspect
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api import routes_sales
from dian_automation.api.app import app
from dian_automation.core import sales_service
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import Business, Sale, Subscription, User


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_owner(db, suffix: str, nit: str, status: str = "ACTIVO") -> None:
    user = User(id=f"usr-{suffix}", email=f"{suffix}@x.co", full_name=suffix, role="CLIENT", is_active=True)
    business = Business(id=f"biz-{suffix}", client_id=user.id, legal_name=suffix, commercial_name=suffix,
                        nit=nit, dv="7", is_active=True)
    sub = Subscription(id=f"sub-{suffix}", client_id=user.id, plan="TRIMESTRAL",
                       discount_rate=Decimal("5.00"), base_price=Decimal("150000.00"),
                       final_price=Decimal("142500.00"), start_date=date.today() - timedelta(days=10),
                       cutoff_date=date.today() + timedelta(days=80),
                       grace_period_end=date.today() + timedelta(days=83), status=status)
    db.add_all([user, business, sub])
    db.commit()


@pytest.fixture
def seeded(db_session):
    _seed_owner(db_session, "ana", "901111111")
    _seed_owner(db_session, "otro", "902222222")
    _seed_owner(db_session, "bloq", "903333333", status="BLOQUEADO")
    return db_session


def _use_db(db) -> None:
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db


@pytest.fixture
def client(seeded, bearer):
    _use_db(seeded)
    yield TestClient(app, headers=bearer("usr-ana"))
    app.dependency_overrides.clear()


def _rows(db) -> int:
    return db.query(Sale).count()


def test_valid_sale_returns_201_and_persists_as_web(client, seeded):
    response = client.post("/api/sales/biz-ana", json={"total_amount": 150000.00, "description": "3 tortas"})

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "total_amount", "description", "recorded_via", "created_at"}
    assert body["total_amount"] == "150000.00"  # cadena Decimal, nunca float
    assert body["description"] == "3 tortas"
    assert body["recorded_via"] == "WEB"
    assert body["created_at"]

    sale = seeded.query(Sale).one()
    assert sale.id == body["id"]
    assert sale.business_id == "biz-ana"
    assert sale.recorded_by_user_id == "usr-ana"
    assert sale.recorded_via == "WEB"
    assert isinstance(sale.total_amount, Decimal)


def test_total_as_string_keeps_exact_decimal(client, seeded):
    response = client.post("/api/sales/biz-ana", json={"total_amount": "100.10"})

    assert response.status_code == 201
    assert response.json()["total_amount"] == "100.10"  # cadena Decimal, nunca float
    assert seeded.query(Sale).one().total_amount == Decimal("100.10")


def test_missing_description_is_null(client, seeded):
    response = client.post("/api/sales/biz-ana", json={"total_amount": 25000})

    assert response.status_code == 201
    assert response.json()["description"] is None
    assert seeded.query(Sale).one().description is None


def test_business_can_be_addressed_by_nit_and_sale_uses_its_id(client, seeded):
    response = client.post("/api/sales/901111111", json={"total_amount": 10})

    assert response.status_code == 201
    assert seeded.query(Sale).one().business_id == "biz-ana"


@pytest.mark.parametrize("total", [0, -5, "0", "-10.50", "abc", "", None, 12.345, "1,5", "$100", 10**20, "1E+99999999", "1E-99999999"])
def test_invalid_total_is_422_and_persists_nothing(client, seeded, total):
    response = client.post("/api/sales/biz-ana", json={"total_amount": total, "description": "x"})

    assert response.status_code == 422
    assert _rows(seeded) == 0


def test_service_validation_error_is_422_with_its_message(client, seeded):
    response = client.post("/api/sales/biz-ana", json={"total_amount": 0})

    assert response.status_code == 422
    assert response.json() == {"detail": "El total debe ser mayor a cero."}


def test_description_over_limit_is_422(client, seeded):
    response = client.post("/api/sales/biz-ana", json={"total_amount": 10, "description": "x" * 501})

    assert response.status_code == 422
    assert "500" in response.json()["detail"]
    assert _rows(seeded) == 0


def test_missing_total_is_422(client, seeded):
    response = client.post("/api/sales/biz-ana", json={"description": "sin total"})

    assert response.status_code == 422
    assert _rows(seeded) == 0


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer no-es-un-jwt"}])
def test_without_valid_session_is_401_and_persists_nothing(seeded, headers):
    _use_db(seeded)
    try:
        response = TestClient(app).post("/api/sales/biz-ana", json={"total_amount": 10}, headers=headers)
        invalid_body = TestClient(app).post("/api/sales/biz-ana", json={"total_amount": 0}, headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert invalid_body.status_code == 401  # la sesión se evalúa antes que el cuerpo
    assert _rows(seeded) == 0


def test_expired_session_is_401(seeded, monkeypatch, bearer):
    headers = bearer("usr-ana")
    _use_db(seeded)
    try:
        from dian_automation.core import auth_service

        monkeypatch.setattr(auth_service, "_utcnow", lambda: auth_service.datetime(2099, 1, 1))
        response = TestClient(app).post("/api/sales/biz-ana", json={"total_amount": 10}, headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert _rows(seeded) == 0


def test_foreign_or_unknown_business_is_404_and_persists_nothing(client, seeded):
    foreign = client.post("/api/sales/biz-otro", json={"total_amount": 10})
    foreign_by_nit = client.post("/api/sales/902222222", json={"total_amount": 10})
    unknown = client.post("/api/sales/biz-no-existe", json={"total_amount": 10})

    assert foreign.status_code == foreign_by_nit.status_code == unknown.status_code == 404
    assert _rows(seeded) == 0


def test_blocked_business_is_403_not_401_and_persists_nothing(seeded, bearer):
    _use_db(seeded)
    try:
        response = TestClient(app, headers=bearer("usr-bloq")).post(
            "/api/sales/biz-bloq", json={"total_amount": 10}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["error"] == "SUBSCRIPTION_BLOCKED"
    assert _rows(seeded) == 0


def test_late_block_detected_by_the_service_is_403(client, seeded, monkeypatch):
    """Bloqueo entre el guardia de acceso y el servicio: 403 con el mismo envelope, 0 filas."""

    def blocked(*_args, **_kwargs):
        raise sales_service.SalesError(sales_service.SalesError.BUSINESS_BLOCKED, "Suscripción suspendida.")

    monkeypatch.setattr(routes_sales.sales_service, "register_sale", blocked)

    response = client.post("/api/sales/biz-ana", json={"total_amount": 10})

    assert response.status_code == 403
    assert response.json()["error"] == "SUBSCRIPTION_BLOCKED"
    assert _rows(seeded) == 0


def test_router_delegates_to_service_with_web_channel_and_jwt_user(client, seeded, monkeypatch):
    fake_sale = SimpleNamespace(
        id="sale-1", total_amount=Decimal("150000.00"), description="3 tortas",
        recorded_via="WEB", created_at=datetime(2026, 9, 18, 10, 0),
    )
    register = MagicMock(return_value=fake_sale)
    monkeypatch.setattr(routes_sales.sales_service, "register_sale", register)

    response = client.post("/api/sales/biz-ana", json={"total_amount": "150000.00", "description": "3 tortas"})

    assert response.status_code == 201
    register.assert_called_once()
    kwargs = register.call_args.kwargs
    assert kwargs["recorded_via"] == "WEB"
    assert kwargs["user"].id == "usr-ana"
    assert kwargs["business"].id == "biz-ana"
    assert kwargs["total"] == Decimal("150000.00")
    assert kwargs["description"] == "3 tortas"
    assert _rows(seeded) == 0  # el router no persiste por su cuenta


def test_router_does_not_validate_or_persist_on_its_own():
    source = inspect.getsource(routes_sales)

    assert "sales_service.register_sale" in source
    for forbidden in ("db.add", "db.commit", "Sale(", "parse_total", "<= 0", "> 0"):
        assert forbidden not in source


def test_auth_and_sales_routers_are_both_mounted(client):
    paths = set(app.openapi()["paths"])
    assert "/api/auth/me" in paths
    assert "/api/sales/{business_id}" in paths

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    sale = client.post(f"/api/sales/{me.json()['business_id']}", json={"total_amount": 10})
    assert sale.status_code == 201
