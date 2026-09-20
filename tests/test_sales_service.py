"""Pruebas del servicio compartido de ventas (Story 2.4): validación, persistencia y bloqueo."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from dian_automation.core import sales_service
from dian_automation.core.sales_service import SalesError, register_sale
from dian_automation.db.models import (
    Base,
    Business,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    Sale,
    Subscription,
    User,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()

    def _client(suffix: str, status: str, income_source: str = INCOME_SOURCE_MANUAL_SALES):
        user = User(id=f"usr-{suffix}", email=f"{suffix}@x.co", full_name=suffix, role="CLIENT",
                    telegram_chat_id=1000 + len(suffix), is_telegram_linked=True, is_active=True)
        biz = Business(id=f"biz-{suffix}", client_id=user.id, legal_name=suffix, commercial_name=suffix,
                       nit="901008579", dv="7", is_active=True, income_source=income_source)
        sub = Subscription(id=f"sub-{suffix}", client_id=user.id, plan="TRIMESTRAL",
                           discount_rate=Decimal("5.00"), base_price=Decimal("150000.00"),
                           final_price=Decimal("142500.00"), start_date=date.today() - timedelta(days=10),
                           cutoff_date=date.today() + timedelta(days=80),
                           grace_period_end=date.today() + timedelta(days=83), status=status)
        return [user, biz, sub]

    session.add_all(
        _client("ok", "ACTIVO")
        + _client("blocked", "BLOQUEADO")
        + _client("dian", "ACTIVO", income_source=INCOME_SOURCE_DIAN)
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _register(db, who="ok", **kwargs):
    kwargs.setdefault("recorded_via", sales_service.RECORDED_VIA_TELEGRAM)
    user = db.get(User, f"usr-{who}")
    business = db.get(Business, f"biz-{who}")
    return register_sale(db, user=user, business=business, **kwargs)


def _count(db) -> int:
    return db.query(Sale).count()


def test_valid_sale_is_persisted_with_context(db):
    sale = _register(db, total="150000", description="3 tortas de chocolate")

    assert sale.business_id == "biz-ok"
    assert sale.recorded_by_user_id == "usr-ok"
    assert sale.recorded_via == "TELEGRAM"
    assert sale.total_amount == Decimal("150000.00")
    assert isinstance(sale.total_amount, Decimal)
    assert sale.description == "3 tortas de chocolate"
    assert sale.created_at is not None


def test_recorded_via_is_parametrizable(db):
    sale = _register(db, total="10", recorded_via=sales_service.RECORDED_VIA_WEB)

    assert sale.recorded_via == "WEB"


def test_recorded_via_is_required(db):
    user, business = db.get(User, "usr-ok"), db.get(Business, "biz-ok")

    with pytest.raises(TypeError):
        register_sale(db, user=user, business=business, total="10")


def test_decimal_precision_is_exact(db):
    sale = _register(db, total="100.10")
    db.expire_all()

    assert db.get(Sale, sale.id).total_amount == Decimal("100.10")


@pytest.mark.parametrize("raw", ["0", "0.00", "-100", "-0.5"])
def test_zero_or_negative_total_is_rejected(db, raw):
    with pytest.raises(SalesError) as exc:
        _register(db, total=raw)

    assert exc.value.code == SalesError.NON_POSITIVE_TOTAL
    assert _count(db) == 0


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "abc", "$150000", "150,5", "150.000,5", "150.000", "100.125", "1e5", ".5", "100.", "١٢٣", "1" * 13, None, 1.5],
)
def test_invalid_total_format_is_rejected(db, raw):
    with pytest.raises(SalesError) as exc:
        _register(db, total=raw)

    assert exc.value.code == SalesError.INVALID_TOTAL
    assert _count(db) == 0


@pytest.mark.parametrize(
    "raw",
    [Decimal("1E+99999999"), Decimal("1E-99999999"), Decimal("-1E+99999999"), Decimal("0E+99999999"),
     Decimal("NaN"), Decimal("Infinity"), Decimal("1E+12"), Decimal("0.001")],
)
def test_decimal_with_extreme_exponent_or_non_finite_is_rejected_without_formatting(raw):
    with pytest.raises(SalesError) as exc:
        sales_service.parse_total(raw)

    assert exc.value.code == SalesError.INVALID_TOTAL


def test_decimal_range_edges_are_accepted():
    assert sales_service.parse_total(Decimal("0.01")) == Decimal("0.01")
    assert sales_service.parse_total(Decimal("999999999999.99")) == Decimal("999999999999.99")


def test_accepted_total_formats(db):
    assert sales_service.parse_total("150000") == Decimal("150000")
    assert sales_service.parse_total(" 99.5 ") == Decimal("99.5")
    assert sales_service.parse_total(Decimal("20.00")) == Decimal("20.00")
    assert sales_service.parse_total(75) == Decimal("75")


@pytest.mark.parametrize("raw, expected", [(None, None), ("", None), ("   ", None), (" a | b ", "a | b"), ("Tortas ÑU", "Tortas ÑU")])
def test_description_normalization(raw, expected):
    assert sales_service.normalize_description(raw) == expected


def test_description_over_500_is_rejected_and_500_is_accepted(db):
    sale = _register(db, total="5", description="x" * 500)
    assert len(sale.description) == 500

    with pytest.raises(SalesError) as exc:
        _register(db, total="5", description="x" * 501)

    assert exc.value.code == SalesError.DESCRIPTION_TOO_LONG
    assert _count(db) == 1


def test_blocked_client_fails_with_block_error_and_no_row(db):
    with pytest.raises(SalesError) as exc:
        _register(db, who="blocked", total="100")

    assert exc.value.code == SalesError.BUSINESS_BLOCKED
    assert _count(db) == 0


def test_check_constraint_rejects_non_positive_total_at_database_level(db):
    db.add(Sale(business_id="biz-ok", total_amount=Decimal("0"), recorded_via="TELEGRAM",
                recorded_by_user_id="usr-ok", sale_date=date(2026, 1, 1)))

    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_register_sale_now_at_0430_utc_stores_previous_day_bogota_date(db):
    # 04:30 UTC del 2026-09-21 = 23:30 Bogotá del 2026-09-20
    now = datetime(2026, 9, 21, 4, 30)
    sale = _register(db, total="50000", now=now)

    assert sale.created_at == now
    assert sale.sale_date == date(2026, 9, 20)


def test_register_sale_now_at_0510_utc_stores_same_day_bogota_date(db):
    # 05:10 UTC del 2026-09-21 = 00:10 Bogotá del 2026-09-21
    now = datetime(2026, 9, 21, 5, 10)
    sale = _register(db, total="60000", now=now)

    assert sale.created_at == now
    assert sale.sale_date == date(2026, 9, 21)


def test_dian_business_fails_with_not_manual_sales_and_no_row(db):
    with pytest.raises(SalesError) as exc:
        _register(db, who="dian", total="100")

    assert exc.value.code == SalesError.NOT_MANUAL_SALES
    assert "Tu negocio factura electrónicamente" in exc.value.message
    assert _count(db) == 0


def test_manual_sales_business_succeeds(db):
    sale = _register(db, who="ok", total="50000", description="Venta manual")

    assert sale.id is not None
    assert sale.total_amount == Decimal("50000.00")
    assert _count(db) == 1

