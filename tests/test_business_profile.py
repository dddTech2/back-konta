"""Pruebas de la Story 10.1: paquete (responsable de IVA) y perfil de ingresos mixto."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api.app import app
from dian_automation.core.business_profile import (
    ProfileError,
    has_einvoicing,
    has_manual_sales,
    package_of,
    shows_iva,
    validate_profile,
)
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import Business, Sale, Subscription, User


# ==============================================================================
# 1. Helpers de perfil
# ==============================================================================


@pytest.mark.parametrize(
    "vat, source, package, einvoicing, sales, iva",
    [
        (True, "DIAN", "CON_IVA", True, False, True),
        (False, "DIAN", "SIN_IVA", True, False, False),
        (False, "MANUAL_SALES", "SIN_IVA", False, True, False),
        (False, "MIXED", "SIN_IVA", True, True, False),
    ],
)
def test_helpers_for_each_valid_combination(vat, source, package, einvoicing, sales, iva):
    biz = Business(income_source=source, vat_responsible=vat)

    validate_profile(vat, source)
    assert package_of(biz) == package
    assert has_einvoicing(biz) is einvoicing
    assert has_manual_sales(biz) is sales
    assert shows_iva(biz) is iva


@pytest.mark.parametrize("source", ["MANUAL_SALES", "MIXED"])
def test_vat_responsible_requires_einvoicing(source):
    with pytest.raises(ProfileError) as exc:
        validate_profile(True, source)
    assert exc.value.code == "VAT_REQUIRES_EINVOICING"


def test_unknown_income_source_is_rejected():
    with pytest.raises(ProfileError) as exc:
        validate_profile(False, "OTRO")
    assert exc.value.code == "INVALID_INCOME_SOURCE"


def test_unsaved_business_derives_vat_like_the_model_default():
    """Sin valor guardado, un facturador es declarante de IVA (igual que el relleno de 0012)."""
    assert package_of(Business(income_source="DIAN")) == "CON_IVA"
    assert package_of(Business(income_source="MIXED")) == "SIN_IVA"
    assert package_of({"income_source": "MANUAL_SALES", "vat_responsible": None}) == "SIN_IVA"


def test_model_default_on_insert(db_session):
    user = User(id="usr-d", email="d@x.co", full_name="d", role="CLIENT", is_active=True)
    dian = Business(id="biz-d1", client_id=user.id, legal_name="a", commercial_name="a", nit="900000001", dv="1",
                    income_source="DIAN")
    mixed = Business(id="biz-d2", client_id=user.id, legal_name="b", commercial_name="b", nit="900000002", dv="1",
                     income_source="MIXED")
    db_session.add_all([user, dian, mixed])
    db_session.commit()

    assert dian.vat_responsible is True
    assert mixed.vat_responsible is False


# ==============================================================================
# 2. Visibilidad en la API
# ==============================================================================


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


def _seed_owner(db, suffix: str, nit: str, income_source: str, vat_responsible: bool) -> None:
    user = User(id=f"usr-{suffix}", email=f"{suffix}@x.co", full_name=suffix, role="CLIENT", is_active=True)
    business = Business(id=f"biz-{suffix}", client_id=user.id, legal_name=suffix, commercial_name=suffix,
                        nit=nit, dv="7", is_active=True, income_source=income_source,
                        vat_responsible=vat_responsible)
    sub = Subscription(id=f"sub-{suffix}", client_id=user.id, plan="TRIMESTRAL",
                       discount_rate=Decimal("5.00"), base_price=Decimal("150000.00"),
                       final_price=Decimal("142500.00"), start_date=date.today() - timedelta(days=10),
                       cutoff_date=date.today() + timedelta(days=80),
                       grace_period_end=date.today() + timedelta(days=83), status="ACTIVO")
    db.add_all([user, business, sub])
    db.commit()


@pytest.fixture
def seeded(db_session):
    _seed_owner(db_session, "mix", "901111111", "MIXED", False)
    _seed_owner(db_session, "dianiva", "902222222", "DIAN", True)
    _seed_owner(db_session, "diansin", "903333333", "DIAN", False)
    _seed_owner(db_session, "ventas", "904444444", "MANUAL_SALES", False)

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    yield db_session
    app.dependency_overrides.clear()


def test_mixed_business_registers_sales_and_sees_the_dian_dashboard(seeded, bearer):
    client = TestClient(app, headers=bearer("usr-mix"))

    sale = client.post("/api/sales/biz-mix", json={"total_amount": 80000, "description": "venta sin factura"})
    dashboard = client.get("/api/dashboard/biz-mix")

    assert sale.status_code == 201
    assert seeded.query(Sale).filter(Sale.business_id == "biz-mix").count() == 1
    assert dashboard.status_code == 200
    assert dashboard.json()["shows_iva"] is False
    assert client.get("/api/iva/biz-mix").status_code == 404


def test_dian_without_vat_hides_iva_but_keeps_the_dashboard(seeded, bearer):
    client = TestClient(app, headers=bearer("usr-diansin"))

    dashboard = client.get("/api/dashboard/biz-diansin")

    assert dashboard.status_code == 200
    assert dashboard.json()["shows_iva"] is False
    assert dashboard.json()["resumen"]["ivaAcumulado"] == 0
    assert client.get("/api/iva/biz-diansin").status_code == 404
    assert client.post("/api/sales/biz-diansin", json={"total_amount": 1000}).status_code == 409


def test_dian_with_vat_keeps_iva(seeded, bearer):
    client = TestClient(app, headers=bearer("usr-dianiva"))

    assert client.get("/api/dashboard/biz-dianiva").json()["shows_iva"] is True
    assert client.get("/api/iva/biz-dianiva").status_code == 200


def test_manual_sales_business_still_has_no_dashboard(seeded, bearer):
    client = TestClient(app, headers=bearer("usr-ventas"))

    assert client.get("/api/dashboard/biz-ventas").status_code == 404


@pytest.mark.parametrize(
    "user_id, income_source, vat, package",
    [("usr-mix", "MIXED", False, "SIN_IVA"), ("usr-dianiva", "DIAN", True, "CON_IVA")],
)
def test_me_reports_package_and_vat(seeded, bearer, user_id, income_source, vat, package):
    body = TestClient(app, headers=bearer(user_id)).get("/api/auth/me").json()

    assert body["income_source"] == income_source
    assert body["vat_responsible"] is vat
    assert body["package"] == package
