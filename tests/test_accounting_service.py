"""Pruebas de la Story 10.3: reparto Visión Contable / NYO y contabilidad de Konta."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.core import accounting_service, admin_service
from dian_automation.core.accounting_service import AccountingError, split_amount
from dian_automation.db.database import Base
from dian_automation.db.models import Business, PaymentRecord, Subscription, User


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    session.add(User(id="usr-admin", email="admin@x.co", full_name="Katerinn", role="ADMIN", is_active=True))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _client(db, suffix, nit, *, vat=True, income_source="DIAN", package="CON_IVA", monthly="349900.00",
            status="ACTIVO", created_at=datetime(2026, 1, 10, 15, 0)):
    user = User(id=f"usr-{suffix}", email=f"{suffix}@x.co", full_name=suffix, role="CLIENT", is_active=True)
    biz = Business(id=f"biz-{suffix}", client_id=user.id, legal_name=suffix, commercial_name=f"Negocio {suffix}",
                   nit=nit, dv="1", income_source=income_source, vat_responsible=vat, is_active=True,
                   created_at=created_at)
    sub = Subscription(id=f"sub-{suffix}", client_id=user.id, plan="TRIMESTRAL", package=package,
                       monthly_price=Decimal(monthly), discount_rate=Decimal("5.00"),
                       base_price=Decimal(monthly) * 3, final_price=Decimal(monthly) * 3,
                       start_date=date(2026, 1, 1), cutoff_date=date(2026, 12, 1),
                       grace_period_end=date(2026, 12, 4), status=status)
    db.add_all([user, biz, sub])
    db.commit()
    return biz, sub


def _payment(db, sub, amount, day, package, nyo):
    db.add(PaymentRecord(subscription_id=sub.id, amount=Decimal(amount), payment_date=day, package=package,
                         split_nyo_pct=Decimal("50.00"), nyo_amount=Decimal(nyo),
                         visioncontable_amount=Decimal(amount) - Decimal(nyo)))
    db.commit()


# ==============================================================================
# 1. split_amount y porcentajes
# ==============================================================================


@pytest.mark.parametrize(
    "amount, pct, nyo, vc",
    [
        ("349900", "50", "174950", "174950"),
        ("249900", "33.33", "83292", "166608"),
        ("249901", "50", "124951", "124950"),  # el peso impar va a NYO por redondeo hacia arriba
        ("142500.50", "50", "71250", "71250.50"),  # las dos partes siempre suman el pago
        ("100000", "0", "0", "100000"),
        ("100000", "100", "100000", "0"),
    ],
)
def test_split_amount(amount, pct, nyo, vc):
    got_nyo, got_vc = split_amount(Decimal(amount), Decimal(pct))

    assert got_nyo == Decimal(nyo)
    assert got_vc == Decimal(vc)
    assert got_nyo + got_vc == Decimal(amount)


def test_default_split_is_50_50_for_both_packages(db):
    rows = accounting_service.get_split(db)

    assert [(r.package, r.nyo_pct, r.visioncontable_pct) for r in rows] == [
        ("CON_IVA", Decimal("50.00"), Decimal("50.00")),
        ("SIN_IVA", Decimal("50.00"), Decimal("50.00")),
    ]


def test_set_split_updates_and_validates(db):
    row = accounting_service.set_split(db, "sin_iva", Decimal("40"), admin=db.get(User, "usr-admin"))
    db.commit()

    assert row.nyo_pct == Decimal("40.00") and row.visioncontable_pct == Decimal("60.00")
    assert row.updated_by_admin_id == "usr-admin"
    for bad in ("-1", "100.01", "33.333"):
        with pytest.raises(AccountingError) as exc:
            accounting_service.set_split(db, "CON_IVA", Decimal(bad))
        assert exc.value.code == "INVALID_SPLIT"
    with pytest.raises(AccountingError) as exc:
        accounting_service.set_split(db, "DIAN", Decimal("50"))
    assert exc.value.code == "INVALID_PACKAGE"


# ==============================================================================
# 2. Reparto congelado al confirmar pago
# ==============================================================================


def test_confirm_payment_freezes_the_split(db):
    biz, sub = _client(db, "ana", "900111222")
    admin = db.get(User, "usr-admin")
    accounting_service.set_split(db, "CON_IVA", Decimal("30"))
    db.commit()

    first = admin_service.confirm_payment(db=db, admin=admin, nit="900111222", amount=Decimal("1049700"),
                                          reference="TR-1")
    accounting_service.set_split(db, "CON_IVA", Decimal("50"))
    db.commit()
    second = admin_service.confirm_payment(db=db, admin=admin, nit="900111222", amount=Decimal("1049700"),
                                           reference="TR-2")

    p1, p2 = first.payment, second.payment
    assert (p1.package, p1.split_nyo_pct, p1.nyo_amount, p1.visioncontable_amount) == (
        "CON_IVA", Decimal("30.00"), Decimal("314910"), Decimal("734790"),
    )
    db.refresh(p1)
    assert p1.nyo_amount == Decimal("314910.00")  # cambiar el porcentaje no recalcula pagos anteriores
    assert p2.split_nyo_pct == Decimal("50.00") and p2.nyo_amount == Decimal("524850")


def test_confirm_payment_uses_business_package_when_subscription_has_none(db):
    _, sub = _client(db, "mix", "900333444", vat=False, income_source="MIXED", package=None, monthly="249900.00")

    result = admin_service.confirm_payment(db=db, admin=db.get(User, "usr-admin"), nit="900333444",
                                           amount=Decimal("749700"), reference="TR-3")

    assert result.payment.package == "SIN_IVA"


# ==============================================================================
# 3. accounting_summary
# ==============================================================================


@pytest.fixture
def ledger(db):
    _, con = _client(db, "con", "900500600")
    _, sin = _client(db, "sin", "900700800", vat=False, income_source="MIXED", package="SIN_IVA",
                     monthly="249900.00", created_at=datetime(2026, 10, 3, 12, 0))
    _client(db, "bloq", "900900900", status="BLOQUEADO")
    _payment(db, con, "1049700", date(2026, 9, 20), "CON_IVA", "524850")
    _payment(db, con, "1049700", date(2026, 10, 5), "CON_IVA", "524850")
    _payment(db, sin, "749700", date(2026, 10, 8), "SIN_IVA", "374850")
    accounting_service.set_split(db, "SIN_IVA", Decimal("40"))
    db.commit()
    return db


def test_summary_for_a_month(ledger):
    s = accounting_service.accounting_summary(ledger, 2026, 10)

    assert s["collected"]["total"] == Decimal("1799400") and s["collected"]["count"] == 2
    assert s["collected"]["by_package"]["SIN_IVA"] == {"total": Decimal("749700"), "count": 1}
    assert s["split"]["nyo"]["total"] == Decimal("899700")
    assert s["split"]["visioncontable"]["total"] == Decimal("899700")
    assert s["clients"]["active_total"] == 2
    assert s["clients"]["by_package"] == {"CON_IVA": 1, "SIN_IVA": 1}
    assert s["clients"]["by_income_source"]["MIXED"] == 1
    assert s["clients"]["new_in_period"] == 1
    assert s["mrr"]["total"] == Decimal("599800.00")
    assert s["mrr_split"]["nyo"]["by_package"]["SIN_IVA"] == Decimal("99960")  # 40 % vigente de 249.900
    assert [m["month"] for m in s["monthly"]][-1] == "2026-10" and len(s["monthly"]) == 12
    sept = next(m for m in s["monthly"] if m["month"] == "2026-09")
    assert sept["CON_IVA"] == Decimal("1049700") and sept["total"] == Decimal("1049700")
    assert [p["commercial_name"] for p in s["payments"]] == ["Negocio sin", "Negocio con"]


def test_summary_for_a_year_and_empty_period(ledger):
    year = accounting_service.accounting_summary(ledger, 2026)
    empty = accounting_service.accounting_summary(ledger, 2025, 3)

    assert year["collected"]["count"] == 3
    assert [m["month"] for m in year["monthly"]] == [f"2026-{m:02d}" for m in range(1, 13)]
    assert empty["collected"]["total"] == 0 and empty["payments"] == []
    with pytest.raises(AccountingError):
        accounting_service.accounting_summary(ledger, 2026, 13)
