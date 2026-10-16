"""Pruebas de rutas de la Story 10.3: /api/admin/accounting y /api/admin/accounting/split."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api.app import app
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import Business, PaymentRecord, Subscription, User


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    session.add_all([
        User(id="usr-admin", email="admin@x.co", full_name="Katerinn", role="ADMIN", is_active=True),
        User(id="usr-cli", email="cli@x.co", full_name="Cliente", role="CLIENT", is_active=True),
        Business(id="biz-cli", client_id="usr-cli", legal_name="Cli", commercial_name="Tienda Cli", nit="900123123",
                 dv="1", income_source="DIAN", vat_responsible=True, is_active=True,
                 created_at=datetime(2026, 10, 2, 15, 0)),
        Subscription(id="sub-cli", client_id="usr-cli", plan="TRIMESTRAL", package="CON_IVA",
                     monthly_price=Decimal("349900.00"), discount_rate=Decimal("5.00"),
                     base_price=Decimal("1049700.00"), final_price=Decimal("997215.00"),
                     start_date=date(2026, 10, 1), cutoff_date=date(2027, 1, 1),
                     grace_period_end=date(2027, 1, 4), status="ACTIVO"),
        PaymentRecord(id="pay-1", subscription_id="sub-cli", amount=Decimal("997215.00"),
                      payment_date=date(2026, 10, 2), package="CON_IVA", split_nyo_pct=Decimal("50.00"),
                      nyo_amount=Decimal("498608.00"), visioncontable_amount=Decimal("498607.00")),
    ])
    session.commit()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield session
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


@pytest.fixture
def admin(db, bearer):
    return TestClient(app, headers=bearer("usr-admin"))


@pytest.mark.parametrize(
    "method, url, body",
    [("GET", "/api/admin/accounting?year=2026", None), ("GET", "/api/admin/accounting/split", None),
     ("PUT", "/api/admin/accounting/split/CON_IVA", {"nyo_pct": "40"})],
)
def test_accounting_routes_require_admin(db, bearer, method, url, body):
    assert TestClient(app).request(method, url, json=body).status_code == 401
    assert TestClient(app, headers=bearer("usr-cli")).request(method, url, json=body).status_code == 403


def test_get_accounting_month(admin):
    res = admin.get("/api/admin/accounting?year=2026&month=10")

    assert res.status_code == 200
    data = res.json()
    assert data["collected"]["total"] == "997215.00" and data["collected"]["count"] == 1
    assert data["collected"]["by_package"]["CON_IVA"] == {"total": "997215.00", "count": 1}
    assert data["split"]["nyo"]["total"] == "498608.00"
    assert data["split"]["visioncontable"]["total"] == "498607.00"
    assert data["clients"]["active_total"] == 1 and data["clients"]["new_in_period"] == 1
    assert data["mrr"]["total"] == "349900.00"
    assert data["mrr_split"]["nyo"]["total"] == "174950.00"
    assert len(data["monthly"]) == 12 and data["monthly"][-1] == {
        "month": "2026-10", "CON_IVA": "997215.00", "SIN_IVA": "0.00", "total": "997215.00",
    }
    assert data["payments"] == [{
        "payment_date": "2026-10-02", "business_id": "biz-cli", "commercial_name": "Tienda Cli",
        "package": "CON_IVA", "amount": "997215.00", "nyo_amount": "498608.00",
        "visioncontable_amount": "498607.00",
    }]


@pytest.mark.parametrize("query", ["", "?year=1999", "?year=2026&month=0", "?year=2026&month=13"])
def test_get_accounting_rejects_bad_period(admin, query):
    assert admin.get(f"/api/admin/accounting{query}").status_code == 422


def test_split_get_and_put(admin):
    initial = admin.get("/api/admin/accounting/split").json()
    assert [(r["package"], r["nyo_pct"], r["visioncontable_pct"]) for r in initial] == [
        ("CON_IVA", "50.00", "50.00"), ("SIN_IVA", "50.00", "50.00"),
    ]

    updated = admin.put("/api/admin/accounting/split/sin_iva", json={"nyo_pct": "35.5"})

    assert updated.status_code == 200
    assert updated.json()["nyo_pct"] == "35.50" and updated.json()["visioncontable_pct"] == "64.50"
    assert admin.get("/api/admin/accounting/split").json()[1]["nyo_pct"] == "35.50"


@pytest.mark.parametrize(
    "package, body, status, code",
    [("CON_IVA", {"nyo_pct": "101"}, 422, "INVALID_SPLIT"), ("CON_IVA", {"nyo_pct": "12.345"}, 422, "INVALID_SPLIT"),
     ("CON_IVA", {"nyo_pct": "abc"}, 422, "INVALID_SPLIT"), ("DIAN", {"nyo_pct": "50"}, 404, "INVALID_PACKAGE")],
)
def test_split_put_validations(admin, package, body, status, code):
    res = admin.put(f"/api/admin/accounting/split/{package}", json=body)

    assert res.status_code == status and res.json()["code"] == code


def test_client_detail_payments_include_the_split(admin):
    payments = admin.get("/api/admin/clients/biz-cli").json()["recent_payments"]

    assert payments[0]["nyo_amount"] == "498608.00"
    assert payments[0]["visioncontable_amount"] == "498607.00"
