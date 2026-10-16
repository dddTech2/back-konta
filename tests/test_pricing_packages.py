"""Pruebas de la Story 10.2: tarifa por paquete (Con IVA / Sin IVA) y paquete congelado en la suscripción."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api.app import app
from dian_automation.core import admin_service
from dian_automation.core.admin_service import NewClientData
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import PricingRate, User
from dian_automation.subscriptions.pricing import PricingError, PricingService, ensure_default_pricing


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    session.add(User(id="usr-admin", email="admin@x.co", full_name="Katerinn", role="ADMIN",
                     telegram_chat_id=555444333, is_active=True))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _admin(db):
    return db.query(User).filter(User.role == "ADMIN").one()


def _create(db, nit, income_source=None, vat_responsible=None, plan="TRIMESTRAL"):
    return admin_service.create_client(
        db,
        _admin(db),
        NewClientData(tipo_cliente="PERSONA", full_name=f"Cliente {nit}", phone=f"300{nit[-7:]}", nit=nit,
                      plan=plan, income_source=income_source, vat_responsible=vat_responsible),
    )


# ==============================================================================
# 1. Semilla y cotizaciones con los precios reales
# ==============================================================================


def test_seed_has_one_rate_per_package_from_2026_10_15(db):
    ensure_default_pricing(db)

    rates = {r.package: r for r in db.query(PricingRate).all()}

    assert rates["CON_IVA"].monthly_price == Decimal("349900.00")
    assert rates["SIN_IVA"].monthly_price == Decimal("249900.00")
    assert {r.effective_from for r in rates.values()} == {date(2026, 10, 15)}
    with pytest.raises(PricingError) as exc:
        PricingService.current_rate(db, "CON_IVA", on=date(2026, 10, 14))
    assert exc.value.code == "NO_RATE"


@pytest.mark.parametrize(
    "package, plan, expected",
    [("SIN_IVA", "TRIMESTRAL", "712215.00"), ("CON_IVA", "ANUAL", "3778920.00"), ("CON_IVA", "SEMESTRAL", "1931448.00")],
)
def test_quote_for_package(db, package, plan, expected):
    quote = PricingService.quote_for_package(db, package, plan, start_date=date(2026, 11, 1))

    assert quote.final_price == Decimal(expected)


def test_rates_with_only_legacy_segment_are_ignored(db):
    ensure_default_pricing(db)
    db.add(PricingRate(id="legacy", income_source="DIAN", taxpayer_type="PERSONA_NATURAL",
                       monthly_price=Decimal("50000.00"), effective_from=date(2026, 10, 20)))
    db.commit()

    assert PricingService.current_rate(db, "CON_IVA", on=date(2026, 10, 25)).monthly_price == Decimal("349900.00")


# ==============================================================================
# 2. Paquete congelado en la suscripción
# ==============================================================================


@pytest.mark.parametrize(
    "income_source, vat, package, final_price",
    [(None, None, "CON_IVA", Decimal("997215.00")), ("MIXTO", None, "SIN_IVA", Decimal("712215.00")),
     ("FACTURADOR", False, "SIN_IVA", Decimal("712215.00"))],
)
def test_create_client_quotes_by_package(db, income_source, vat, package, final_price):
    created = _create(db, "1000000101", income_source, vat)

    assert created.subscription.package == package
    assert created.package == package
    assert created.subscription.final_price == final_price


def test_changing_package_keeps_price_until_back_to_current_rate(db):
    created = _create(db, "1000000102", "MIXTO")
    sub = created.subscription
    assert sub.package == "SIN_IVA"

    admin_service.set_business_profile(db, created.business.id, income_source="FACTURADOR", vat_responsible=True)
    db.refresh(sub)
    assert sub.package == "SIN_IVA" and sub.monthly_price == Decimal("249900.00")

    detail = admin_service.get_client_detail(db=db, business_id=created.business.id, admin=_admin(db))
    assert detail["subscription"]["package"] == "SIN_IVA"
    assert detail["subscription"]["current_package"] == "CON_IVA"
    assert detail["subscription"]["current_rate_monthly"] == "349900.00"

    PricingService.apply_to_subscription(db, sub, use_current_rate=True, reason="Pasa a declarante de IVA",
                                         admin=_admin(db))
    db.commit()
    db.refresh(sub)
    assert sub.package == "CON_IVA"
    assert sub.monthly_price == Decimal("349900.00")
    assert sub.final_price == Decimal("997215.00")


def test_new_rate_applies_only_to_new_clients_of_that_package(db):
    first = _create(db, "1000000103", "MIXTO")
    # Tarifa nueva de Sin IVA desde hoy (si hoy es la fecha de la semilla, se reemplaza ese valor, como hace la API).
    same_day = db.query(PricingRate).filter(PricingRate.package == "SIN_IVA",
                                            PricingRate.effective_from == date.today()).first()
    if same_day:
        same_day.monthly_price = Decimal("299900.00")
    else:
        db.add(PricingRate(id="sin-iva-up", package="SIN_IVA", monthly_price=Decimal("299900.00"),
                           effective_from=date.today()))
    db.commit()

    second = _create(db, "1000000104", "VENTAS_MANUALES")
    with_vat = _create(db, "1000000105")

    db.refresh(first.subscription)
    assert first.subscription.monthly_price == Decimal("249900.00")
    assert second.subscription.monthly_price == Decimal("299900.00")
    assert with_vat.subscription.monthly_price == Decimal("349900.00")


# ==============================================================================
# 3. API por paquete
# ==============================================================================


@pytest.fixture
def api(db, bearer):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app, headers=bearer("usr-admin"))
    app.dependency_overrides.clear()


def test_api_overview_with_real_prices(api):
    data = api.get("/api/admin/pricing").json()

    assert [(r["package"], r["monthly_price"]) for r in data["rates"]] == [
        ("CON_IVA", "349900.00"), ("SIN_IVA", "249900.00"),
    ]
    sin_iva_trimestral = next(q for q in data["quotes"] if q["package"] == "SIN_IVA" and q["plan"] == "TRIMESTRAL")
    assert sin_iva_trimestral["final_price"] == "712215.00"


def test_api_rejects_unknown_package_and_quote_without_package(api):
    bad_rate = api.post("/api/admin/pricing/rates", json={"package": "DIAN", "monthly_price": "300000"})
    no_package = api.get("/api/admin/pricing/quote?plan=TRIMESTRAL")
    bad_history = api.get("/api/admin/pricing/rates/history?package=OTRO")

    assert bad_rate.status_code == 422 and bad_rate.json()["code"] == "INVALID_PACKAGE"
    assert no_package.status_code == 422
    assert bad_history.status_code == 422


def test_api_quote_by_vat_responsible(api):
    quote = api.get("/api/admin/pricing/quote?vat_responsible=false&plan=TRIMESTRAL").json()

    assert quote["package"] == "SIN_IVA"
    assert quote["final_price"] == "712215.00"
