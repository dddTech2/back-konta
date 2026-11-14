"""Pruebas de las rutas de gastos `/api/expenses` (Story 11.1): adaptador HTTP sobre `core/expense_service.py`."""

import inspect
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api import routes_expenses
from dian_automation.api.app import app
from dian_automation.core import expense_service
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import (
    Business,
    Expense,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    INCOME_SOURCE_MIXED,
    Subscription,
    User,
)

_BOGOTA_TZ = ZoneInfo("America/Bogota")


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


def _seed_owner(
    db, suffix: str, nit: str, status: str = "ACTIVO", income_source: str = INCOME_SOURCE_MANUAL_SALES
) -> None:
    user = User(id=f"usr-{suffix}", email=f"{suffix}@x.co", full_name=suffix, role="CLIENT", is_active=True)
    business = Business(
        id=f"biz-{suffix}", client_id=user.id, legal_name=suffix, commercial_name=suffix,
        nit=nit, dv="7", is_active=True, income_source=income_source,
    )
    sub = Subscription(
        id=f"sub-{suffix}", client_id=user.id, plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"), base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"), start_date=date.today() - timedelta(days=10),
        cutoff_date=date.today() + timedelta(days=80),
        grace_period_end=date.today() + timedelta(days=83), status=status,
    )
    db.add_all([user, business, sub])
    db.commit()


@pytest.fixture
def seeded(db_session):
    _seed_owner(db_session, "ana", "901111111", income_source=INCOME_SOURCE_MANUAL_SALES)
    _seed_owner(db_session, "mixed", "902222222", income_source=INCOME_SOURCE_MIXED)
    _seed_owner(db_session, "dian", "903333333", income_source=INCOME_SOURCE_DIAN)
    _seed_owner(db_session, "bloq", "904444444", status="BLOQUEADO", income_source=INCOME_SOURCE_MANUAL_SALES)
    _seed_owner(db_session, "otro", "905555555", income_source=INCOME_SOURCE_MANUAL_SALES)
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
    return db.query(Expense).count()


# ==============================================================================
# POST /api/expenses/{business_id}
# ==============================================================================


def test_post_expense_success_without_date(client, seeded):
    response = client.post(
        "/api/expenses/biz-ana",
        json={"total_amount": 75000.00, "description": "Harina y azúcar"},
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "total_amount", "description", "recorded_via", "expense_date", "created_at"}
    assert body["total_amount"] == "75000.00"
    assert body["description"] == "Harina y azúcar"
    assert body["recorded_via"] == "WEB"
    assert body["expense_date"] == datetime.now(_BOGOTA_TZ).strftime("%Y-%m-%d")
    assert body["created_at"]

    expense = seeded.query(Expense).one()
    assert expense.id == body["id"]
    assert expense.business_id == "biz-ana"
    assert expense.recorded_by_user_id == "usr-ana"
    assert expense.recorded_via == "WEB"
    assert isinstance(expense.total_amount, Decimal)


def test_post_expense_success_with_previous_month_date(client, seeded):
    today = datetime.now(_BOGOTA_TZ).date()
    prev_year = today.year - 1 if today.month == 1 else today.year
    prev_month = 12 if today.month == 1 else today.month - 1
    prev_date = date(prev_year, prev_month, 1)

    response = client.post(
        "/api/expenses/biz-ana",
        json={"total_amount": "30000.00", "expense_date": prev_date.isoformat()},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["expense_date"] == prev_date.isoformat()
    assert body["total_amount"] == "30000.00"


def test_post_expense_mixed_business_success(seeded, bearer):
    _use_db(seeded)
    try:
        mixed_client = TestClient(app, headers=bearer("usr-mixed"))
        response = mixed_client.post(
            "/api/expenses/biz-mixed",
            json={"total_amount": "45000.00", "description": "Cajas"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["total_amount"] == "45000.00"


def test_post_expense_dian_business_returns_404(seeded, bearer):
    _use_db(seeded)
    try:
        dian_client = TestClient(app, headers=bearer("usr-dian"))
        response = dian_client.post(
            "/api/expenses/biz-dian",
            json={"total_amount": 50000.00, "description": "Gasto"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert _rows(seeded) == 0


def test_post_expense_blocked_service_returns_409(client, seeded, monkeypatch):
    def blocked(*_args, **_kwargs):
        raise expense_service.ExpenseError(
            expense_service.ExpenseError.BUSINESS_BLOCKED,
            "Suscripción suspendida por pago pendiente: no se pueden registrar gastos.",
        )

    monkeypatch.setattr(routes_expenses.expense_service, "register_expense", blocked)
    response = client.post("/api/expenses/biz-ana", json={"total_amount": 10})

    assert response.status_code == 409
    assert "Suscripción suspendida" in response.json()["detail"]
    assert _rows(seeded) == 0


def test_post_expense_blocked_subscription_returns_403(seeded, bearer):
    _use_db(seeded)
    try:
        bloq_client = TestClient(app, headers=bearer("usr-bloq"))
        response = bloq_client.post("/api/expenses/biz-bloq", json={"total_amount": 10})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["error"] == "SUBSCRIPTION_BLOCKED"


def test_post_expense_date_out_of_range_returns_422(client, seeded):
    response = client.post(
        "/api/expenses/biz-ana",
        json={"total_amount": 50000, "expense_date": "2020-01-01"},
    )
    assert response.status_code == 422
    assert "Solo puedes registrar con fecha de este mes o del mes anterior" in response.json()["detail"]
    assert _rows(seeded) == 0


def test_post_expense_invalid_date_format_returns_422(client, seeded):
    response = client.post(
        "/api/expenses/biz-ana",
        json={"total_amount": 50000, "expense_date": "no-es-fecha"},
    )
    assert response.status_code == 422
    assert "Formato de fecha inválido" in response.json()["detail"]
    assert _rows(seeded) == 0


@pytest.mark.parametrize("total", [0, -10, "abc", "", None, 12.345])
def test_post_expense_invalid_total_returns_422(client, seeded, total):
    response = client.post("/api/expenses/biz-ana", json={"total_amount": total})
    assert response.status_code == 422
    assert _rows(seeded) == 0


def test_post_expense_description_too_long_returns_422(client, seeded):
    response = client.post(
        "/api/expenses/biz-ana",
        json={"total_amount": 10, "description": "a" * 501},
    )
    assert response.status_code == 422
    assert "500" in response.json()["detail"]
    assert _rows(seeded) == 0


# ==============================================================================
# GET /api/expenses/{business_id}
# ==============================================================================


def test_get_expenses_list_success(client, seeded):
    e1 = Expense(
        id="e-may-1", business_id="biz-ana", total_amount=Decimal("15000.00"),
        description="Café", recorded_via="WEB", recorded_by_user_id="usr-ana",
        expense_date=date(2026, 5, 10), created_at=datetime(2026, 5, 10, 9, 0, 0),
    )
    e2 = Expense(
        id="e-may-2", business_id="biz-ana", total_amount=Decimal("25000.00"),
        description=None, recorded_via="TELEGRAM", recorded_by_user_id="usr-ana",
        expense_date=date(2026, 5, 15), created_at=datetime(2026, 5, 15, 11, 0, 0),
    )
    e_void = Expense(
        id="e-may-void", business_id="biz-ana", total_amount=Decimal("80000.00"),
        description="Anulado", recorded_via="WEB", recorded_by_user_id="usr-ana",
        expense_date=date(2026, 5, 12), created_at=datetime(2026, 5, 12, 10, 0, 0),
        voided_at=datetime(2026, 5, 12, 12, 0, 0), voided_by_user_id="usr-ana",
    )
    seeded.add_all([e1, e2, e_void])
    seeded.commit()

    response = client.get("/api/expenses/biz-ana?month=2026-05")
    assert response.status_code == 200
    body = response.json()
    assert body["month"] == "2026-05"
    assert len(body["expenses"]) == 2

    expected_keys = {"id", "total_amount", "description", "recorded_via", "expense_date", "created_at"}
    for item in body["expenses"]:
        assert set(item) == expected_keys
        assert isinstance(item["total_amount"], str)

    # Orden expense_date desc, created_at desc
    assert body["expenses"][0]["id"] == "e-may-2"
    assert body["expenses"][0]["total_amount"] == "25000.00"
    assert body["expenses"][1]["id"] == "e-may-1"
    assert body["expenses"][1]["total_amount"] == "15000.00"
    assert "e-may-void" not in [e["id"] for e in body["expenses"]]


def test_get_expenses_list_dian_business_returns_404(seeded, bearer):
    _use_db(seeded)
    try:
        dian_client = TestClient(app, headers=bearer("usr-dian"))
        response = dian_client.get("/api/expenses/biz-dian?month=2026-05")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"] == "Este servicio no aplica a tu tipo de negocio."


def test_get_expenses_list_invalid_month_returns_422(client, seeded):
    response = client.get("/api/expenses/biz-ana?month=2026-13")
    assert response.status_code == 422
    assert "Formato de mes inválido" in response.json()["detail"]


def test_get_expenses_list_defaults_to_bogota_current_month(client, seeded):
    response = client.get("/api/expenses/biz-ana")
    assert response.status_code == 200
    expected = datetime.now(_BOGOTA_TZ).strftime("%Y-%m")
    assert response.json()["month"] == expected


# ==============================================================================
# POST /api/expenses/{business_id}/{expense_id}/void
# ==============================================================================


def test_void_expense_success_marks_row_and_returns_200(client, seeded):
    create_res = client.post("/api/expenses/biz-ana", json={"total_amount": 50000.00, "description": "Gasto"})
    assert create_res.status_code == 201
    expense_id = create_res.json()["id"]

    response = client.post(f"/api/expenses/biz-ana/{expense_id}/void")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "voided_at"}
    assert body["id"] == expense_id
    assert body["voided_at"] is not None

    expense = seeded.get(Expense, expense_id)
    assert expense.voided_at is not None
    assert expense.voided_by_user_id == "usr-ana"


def test_void_expense_second_time_returns_409(client, seeded):
    create_res = client.post("/api/expenses/biz-ana", json={"total_amount": 20000.00})
    expense_id = create_res.json()["id"]

    res1 = client.post(f"/api/expenses/biz-ana/{expense_id}/void")
    assert res1.status_code == 200

    res2 = client.post(f"/api/expenses/biz-ana/{expense_id}/void")
    assert res2.status_code == 409
    assert res2.json()["detail"] == "Ese gasto ya está anulado."


def test_void_expense_nonexistent_returns_404(client, seeded):
    response = client.post("/api/expenses/biz-ana/nonexistent-id/void")
    assert response.status_code == 404
    assert response.json()["detail"] == "No encontramos ese gasto."


def test_void_expense_dian_business_returns_404(seeded, bearer):
    e_dian = Expense(
        id="e-dian-void", business_id="biz-dian", total_amount=Decimal("40000.00"),
        recorded_via="WEB", recorded_by_user_id="usr-dian", expense_date=date(2026, 9, 20),
        created_at=datetime(2026, 9, 20, 10, 0, 0),
    )
    seeded.add(e_dian)
    seeded.commit()

    _use_db(seeded)
    try:
        dian_client = TestClient(app, headers=bearer("usr-dian"))
        response = dian_client.post(f"/api/expenses/biz-dian/{e_dian.id}/void")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_void_expense_blocked_service_returns_409(client, seeded, monkeypatch):
    def blocked(*_args, **_kwargs):
        raise expense_service.ExpenseError(
            expense_service.ExpenseError.BUSINESS_BLOCKED,
            "Suscripción suspendida por pago pendiente: no se pueden anular gastos.",
        )

    monkeypatch.setattr(routes_expenses.expense_service, "void_expense", blocked)
    response = client.post("/api/expenses/biz-ana/any-id/void")

    assert response.status_code == 409
    assert "Suscripción suspendida" in response.json()["detail"]


# ==============================================================================
# Contrato y aislamiento
# ==============================================================================


def test_expenses_router_does_not_validate_or_persist_on_its_own():
    source = inspect.getsource(routes_expenses)

    assert "expense_service.register_expense" in source
    for forbidden in ("db.add", "db.commit", "Expense(", "parse_total", "<= 0", "> 0"):
        assert forbidden not in source


def test_expenses_router_mounted_in_app(client):
    paths = set(app.openapi()["paths"])
    assert "/api/expenses/{business_id}" in paths
    assert "/api/expenses/{business_id}/{expense_id}/void" in paths
