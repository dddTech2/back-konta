"""Pruebas del servicio de ingresos y egresos (Story 6.2)."""

import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.core.income_service import IncomeError, history, summary
from dian_automation.core.sales_service import RECORDED_VIA_TELEGRAM, register_sale
from dian_automation.db.models import Base, Business, Invoice, Sale, User


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def business_a(db):
    user = User(id="usr-a", email="a@test.co", full_name="User A", role="CLIENT", is_active=True)
    biz = Business(
        id="biz-a",
        client_id=user.id,
        legal_name="Biz A SAS",
        commercial_name="Biz A",
        nit="900111222",
        dv="1",
        is_active=True,
    )
    db.add_all([user, biz])
    db.commit()
    return biz


@pytest.fixture
def business_b(db):
    user = User(id="usr-b", email="b@test.co", full_name="User B", role="CLIENT", is_active=True)
    biz = Business(
        id="biz-b",
        client_id=user.id,
        legal_name="Biz B SAS",
        commercial_name="Biz B",
        nit="900333444",
        dv="2",
        is_active=True,
    )
    db.add_all([user, biz])
    db.commit()
    return biz


def _create_sale(db, business, total: Decimal, sale_date: date, user_id: str = "usr-a") -> Sale:
    sale = Sale(
        business_id=business.id,
        total_amount=total,
        recorded_via="TELEGRAM",
        recorded_by_user_id=user_id,
        sale_date=sale_date,
        created_at=datetime.utcnow(),
    )
    db.add(sale)
    db.commit()
    return sale


def _create_invoice(
    db,
    business,
    total: Decimal,
    issue_date: datetime,
    *,
    group_type: str = "Recibido",
    document_type: str = "Factura electrónica de venta",
    iva: Decimal = Decimal("0.00"),
) -> Invoice:
    inv = Invoice(
        id=str(uuid.uuid4()),
        business_id=business.id,
        cufe=str(uuid.uuid4()),
        document_type=document_type,
        group_type=group_type,
        issue_date=issue_date,
        issuer_nit="900999888",
        issuer_name="Proveedor SAS",
        receiver_nit=business.nit,
        receiver_name=business.legal_name,
        total=total,
        iva=iva,
    )
    db.add(inv)
    db.commit()
    return inv


def test_summary_ingresos_only_includes_sales_of_the_requested_month(db, business_a):
    _create_sale(db, business_a, Decimal("100000.00"), date(2026, 4, 30))
    _create_sale(db, business_a, Decimal("250000.00"), date(2026, 5, 1))
    _create_sale(db, business_a, Decimal("150000.00"), date(2026, 5, 31))
    _create_sale(db, business_a, Decimal("300000.00"), date(2026, 6, 1))

    res = summary(db, business_a, "2026-05")

    assert res["month"] == "2026-05"
    assert res["ingresos"] == Decimal("400000.00")
    assert res["egresos"] == Decimal("0.00")
    assert res["utilidad"] == Decimal("400000.00")


def test_sales_at_month_boundary_in_bogota_time(db, business_a):
    user = db.get(User, "usr-a")

    # 2026-06-01 04:30 UTC = 2026-05-31 23:30 Bogotá -> Debe quedar en mayo (2026-05)
    sale_may = register_sale(
        db,
        user=user,
        business=business_a,
        total=Decimal("50000.00"),
        recorded_via=RECORDED_VIA_TELEGRAM,
        now=datetime(2026, 6, 1, 4, 30),
    )
    assert sale_may.sale_date == date(2026, 5, 31)

    # 2026-06-01 05:10 UTC = 2026-06-01 00:10 Bogotá -> Debe quedar en junio (2026-06)
    sale_jun = register_sale(
        db,
        user=user,
        business=business_a,
        total=Decimal("70000.00"),
        recorded_via=RECORDED_VIA_TELEGRAM,
        now=datetime(2026, 6, 1, 5, 10),
    )
    assert sale_jun.sale_date == date(2026, 6, 1)

    summary_may = summary(db, business_a, "2026-05")
    summary_jun = summary(db, business_a, "2026-06")

    assert summary_may["ingresos"] == Decimal("50000.00")
    assert summary_jun["ingresos"] == Decimal("70000.00")


def test_summary_egresos_includes_total_with_iva_of_received_invoices(db, business_a):
    # Total de factura en DIAN incluye IVA: base 100000 + iva 19000 = total 119000
    _create_invoice(
        db,
        business_a,
        total=Decimal("119000.00"),
        iva=Decimal("19000.00"),
        issue_date=datetime(2026, 5, 15, 10, 0, 0),
        group_type="Recibido",
    )
    _create_invoice(
        db,
        business_a,
        total=Decimal("59500.00"),
        iva=Decimal("9500.00"),
        issue_date=datetime(2026, 5, 20, 14, 30, 0),
        group_type="Recibido",
    )

    res = summary(db, business_a, "2026-05")

    assert res["egresos"] == Decimal("178500.00")
    assert res["ingresos"] == Decimal("0.00")
    assert res["utilidad"] == Decimal("-178500.00")


def test_summary_credit_note_subtracts_from_egresos(db, business_a):
    _create_invoice(
        db,
        business_a,
        total=Decimal("100000.00"),
        issue_date=datetime(2026, 5, 10, 10, 0, 0),
        group_type="Recibido",
        document_type="Factura electrónica de venta",
    )
    # Nota de crédito con tilde
    _create_invoice(
        db,
        business_a,
        total=Decimal("20000.00"),
        issue_date=datetime(2026, 5, 12, 11, 0, 0),
        group_type="Recibido",
        document_type="Nota de crédito electrónica",
    )
    # Nota de crédito sin tilde
    _create_invoice(
        db,
        business_a,
        total=Decimal("10000.00"),
        issue_date=datetime(2026, 5, 14, 12, 0, 0),
        group_type="Recibido",
        document_type="Nota de credito",
    )

    res = summary(db, business_a, "2026-05")

    # 100000 - 20000 - 10000 = 70000
    assert res["egresos"] == Decimal("70000.00")


def test_summary_emitido_invoices_do_not_count_as_egresos(db, business_a):
    _create_invoice(
        db,
        business_a,
        total=Decimal("500000.00"),
        issue_date=datetime(2026, 5, 10, 10, 0, 0),
        group_type="Emitido",
        document_type="Factura electrónica de venta",
    )

    res = summary(db, business_a, "2026-05")

    assert res["egresos"] == Decimal("0.00")
    assert res["ingresos"] == Decimal("0.00")
    assert res["utilidad"] == Decimal("0.00")


def test_summary_empty_month_returns_zeros_without_error(db, business_a):
    res = summary(db, business_a, "2026-08")

    assert res == {
        "month": "2026-08",
        "ingresos": Decimal("0.00"),
        "egresos": Decimal("0.00"),
        "utilidad": Decimal("0.00"),
    }
    assert isinstance(res["ingresos"], Decimal)
    assert isinstance(res["egresos"], Decimal)
    assert isinstance(res["utilidad"], Decimal)


def test_history_six_months_ascending_with_year_boundary(db, business_a):
    _create_sale(db, business_a, Decimal("10000.00"), date(2025, 11, 15))
    _create_sale(db, business_a, Decimal("20000.00"), date(2025, 12, 20))
    _create_sale(db, business_a, Decimal("30000.00"), date(2026, 1, 10))
    _create_sale(db, business_a, Decimal("40000.00"), date(2026, 2, 5))

    hist = history(db, business_a, "2026-02", months=6)

    assert len(hist) == 6
    months = [item["month"] for item in hist]
    assert months == ["2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02"]

    assert hist[0]["ingresos"] == Decimal("0.00")
    assert hist[1]["ingresos"] == Decimal("0.00")
    assert hist[2]["ingresos"] == Decimal("10000.00")
    assert hist[3]["ingresos"] == Decimal("20000.00")
    assert hist[4]["ingresos"] == Decimal("30000.00")
    assert hist[5]["ingresos"] == Decimal("40000.00")


@pytest.mark.parametrize("invalid_month", ["2026-13", "2026-1", "abc", "2026/05", "2026-00", "", None, 202605])
def test_invalid_month_format_raises_income_error(db, business_a, invalid_month):
    with pytest.raises(IncomeError) as exc_summary:
        summary(db, business_a, invalid_month)
    assert exc_summary.value.code == IncomeError.INVALID_MONTH

    with pytest.raises(IncomeError) as exc_history:
        history(db, business_a, invalid_month)
    assert exc_history.value.code == IncomeError.INVALID_MONTH


def test_cents_precision_without_rounding_errors(db, business_a):
    _create_sale(db, business_a, Decimal("10.33"), date(2026, 5, 5))
    _create_sale(db, business_a, Decimal("20.33"), date(2026, 5, 10))
    _create_sale(db, business_a, Decimal("30.34"), date(2026, 5, 15))

    _create_invoice(
        db,
        business_a,
        total=Decimal("15.15"),
        issue_date=datetime(2026, 5, 8, 9, 0),
        group_type="Recibido",
    )
    _create_invoice(
        db,
        business_a,
        total=Decimal("25.25"),
        issue_date=datetime(2026, 5, 12, 10, 0),
        group_type="Recibido",
    )

    res = summary(db, business_a, "2026-05")

    # Ingresos: 10.33 + 20.33 + 30.34 = 61.00
    assert res["ingresos"] == Decimal("61.00")
    # Egresos: 15.15 + 25.25 = 40.40
    assert res["egresos"] == Decimal("40.40")
    # Utilidad: 61.00 - 40.40 = 20.60
    assert res["utilidad"] == Decimal("20.60")
    assert isinstance(res["ingresos"], Decimal)
    assert isinstance(res["egresos"], Decimal)
    assert isinstance(res["utilidad"], Decimal)


def test_business_data_is_strictly_isolated(db, business_a, business_b):
    _create_sale(db, business_a, Decimal("100000.00"), date(2026, 5, 10), user_id="usr-a")
    _create_invoice(
        db,
        business_a,
        total=Decimal("40000.00"),
        issue_date=datetime(2026, 5, 15, 10, 0),
        group_type="Recibido",
    )

    _create_sale(db, business_b, Decimal("500000.00"), date(2026, 5, 10), user_id="usr-b")
    _create_invoice(
        db,
        business_b,
        total=Decimal("200000.00"),
        issue_date=datetime(2026, 5, 15, 10, 0),
        group_type="Recibido",
    )

    res_a = summary(db, business_a, "2026-05")
    assert res_a["ingresos"] == Decimal("100000.00")
    assert res_a["egresos"] == Decimal("40000.00")
    assert res_a["utilidad"] == Decimal("60000.00")

    res_b = summary(db, business_b, "2026-05")
    assert res_b["ingresos"] == Decimal("500000.00")
    assert res_b["egresos"] == Decimal("200000.00")
    assert res_b["utilidad"] == Decimal("300000.00")
