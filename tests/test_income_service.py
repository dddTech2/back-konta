"""Pruebas del servicio de ingresos y egresos (Story 6.2)."""

import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.core.income_service import IncomeError, history, summary
from dian_automation.core.sales_service import RECORDED_VIA_TELEGRAM, register_sale
from dian_automation.db.models import Base, Business, INCOME_SOURCE_MANUAL_SALES, Invoice, Sale, User


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
        income_source=INCOME_SOURCE_MANUAL_SALES,
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
        income_source=INCOME_SOURCE_MANUAL_SALES,
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

    assert {k: res[k] for k in ("month", "ingresos", "egresos", "utilidad")} == {
        "month": "2026-08",
        "ingresos": Decimal("0.00"),
        "egresos": Decimal("0.00"),
        "utilidad": Decimal("0.00"),
    }
    # Story 10.4: los campos de contraste también quedan en cero
    assert res["emitido"] == res["ventas"] == res["diferencia"] == Decimal("0.00")
    assert res["emitido_count"] == res["ventas_count"] == res["clientes_count"] == 0
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


def test_voided_sale_is_excluded_from_income_utility_and_history_and_keeps_row(db, business_a):
    sale1 = _create_sale(db, business_a, Decimal("100000.00"), date(2026, 5, 5))
    sale2 = _create_sale(db, business_a, Decimal("50000.00"), date(2026, 5, 10))
    _create_invoice(
        db,
        business_a,
        total=Decimal("30000.00"),
        issue_date=datetime(2026, 5, 8, 9, 0),
        group_type="Recibido",
    )

    pre_res = summary(db, business_a, "2026-05")
    assert pre_res["ingresos"] == Decimal("150000.00")
    assert pre_res["utilidad"] == Decimal("120000.00")

    sale2.voided_at = datetime(2026, 5, 11, 10, 0, 0)
    sale2.voided_by_user_id = "usr-a"
    db.commit()

    post_res = summary(db, business_a, "2026-05")
    assert post_res["ingresos"] == Decimal("100000.00")
    assert post_res["egresos"] == Decimal("30000.00")
    assert post_res["utilidad"] == Decimal("70000.00")

    hist = history(db, business_a, "2026-05", months=2)
    assert len(hist) == 2
    may_item = next(item for item in hist if item["month"] == "2026-05")
    assert may_item["ingresos"] == Decimal("100000.00")
    assert may_item["utilidad"] == Decimal("70000.00")

    db_sale2 = db.get(Sale, sale2.id)
    assert db_sale2 is not None
    assert db_sale2.voided_at is not None
    assert db.query(Sale).filter(Sale.business_id == business_a.id).count() == 2


def test_voiding_previous_month_sale_recalculates_that_month_and_history(db, business_a):
    sale_apr = _create_sale(db, business_a, Decimal("200000.00"), date(2026, 4, 15))
    sale_may = _create_sale(db, business_a, Decimal("100000.00"), date(2026, 5, 10))

    res_apr = summary(db, business_a, "2026-04")
    assert res_apr["ingresos"] == Decimal("200000.00")

    sale_apr.voided_at = datetime(2026, 5, 12, 9, 0, 0)
    sale_apr.voided_by_user_id = "usr-a"
    db.commit()

    res_apr_after = summary(db, business_a, "2026-04")
    assert res_apr_after["ingresos"] == Decimal("0.00")
    assert res_apr_after["utilidad"] == Decimal("0.00")

    res_may_after = summary(db, business_a, "2026-05")
    assert res_may_after["ingresos"] == Decimal("100000.00")

    hist = history(db, business_a, "2026-05", months=2)
    apr_item = next(item for item in hist if item["month"] == "2026-04")
    may_item = next(item for item in hist if item["month"] == "2026-05")
    assert apr_item["ingresos"] == Decimal("0.00")
    assert may_item["ingresos"] == Decimal("100000.00")

    assert db.get(Sale, sale_apr.id) is not None


# ==============================================================================
# Story 10.4: perfil mixto (facturado vs ventas)
# ==============================================================================


def _emitted(db, business, total, issue_date, receiver_nit, document_type="Factura electrónica de venta"):
    db.add(Invoice(id=str(uuid.uuid4()), business_id=business.id, cufe=str(uuid.uuid4()),
                   document_type=document_type, group_type="Emitido", issue_date=issue_date,
                   issuer_nit=business.nit, issuer_name=business.legal_name, receiver_nit=receiver_nit,
                   receiver_name=f"Cliente {receiver_nit}", total=total, iva=Decimal("0.00")))
    db.commit()


@pytest.fixture
def mixed(db):
    user = User(id="usr-m", email="m@test.co", full_name="User M", role="CLIENT", is_active=True)
    biz = Business(id="biz-m", client_id=user.id, legal_name="Mixto SAS", commercial_name="Mixto",
                   nit="900555666", dv="3", income_source="MIXED", vat_responsible=False, is_active=True)
    db.add_all([user, biz])
    db.commit()
    _emitted(db, biz, Decimal("600000.00"), datetime(2026, 9, 3, 15), "800111")
    _emitted(db, biz, Decimal("300000.00"), datetime(2026, 9, 10, 15), "800222")
    _emitted(db, biz, Decimal("100000.00"), datetime(2026, 9, 12, 15), "800111")
    _emitted(db, biz, Decimal("50000.00"), datetime(2026, 9, 20, 15), "800222", "Nota crédito electrónica")
    _emitted(db, biz, Decimal("999999.00"), datetime(2026, 10, 1, 15), "800333")  # otro mes
    _create_sale(db, biz, Decimal("200000.00"), date(2026, 9, 5), user_id="usr-m")
    _create_sale(db, biz, Decimal("150000.00"), date(2026, 9, 6), user_id="usr-m")
    voided = _create_sale(db, biz, Decimal("90000.00"), date(2026, 9, 7), user_id="usr-m")
    voided.voided_at = datetime(2026, 9, 8)
    _create_invoice(db, biz, Decimal("400000.00"), datetime(2026, 9, 15, 15))
    return biz


def test_mixed_summary_contrasts_emitted_and_sales(db, mixed):
    res = summary(db, mixed, "2026-09")

    assert res["emitido"] == Decimal("950000.00")  # 600k + 300k + 100k − nota crédito 50k
    assert res["ventas"] == Decimal("350000.00")  # sin la anulada
    # Q-10b: facturado y ventas son independientes; no hay ingresos totales ni utilidad
    assert res["ingresos"] is None and res["utilidad"] is None
    assert res["egresos"] == Decimal("400000.00")
    assert res["diferencia"] == Decimal("600000.00")
    assert "emitido_pct" not in res and "ventas_pct" not in res
    assert (res["emitido_count"], res["ventas_count"], res["clientes_count"]) == (3, 2, 2)


def test_summary_accepts_the_business_id(db, mixed):
    assert summary(db, "biz-m", "2026-09")["ventas"] == Decimal("350000.00")


def test_manual_sales_income_ignores_emitted_invoices(db, business_a):
    _emitted(db, business_a, Decimal("500000.00"), datetime(2026, 9, 3, 15), "800111")
    _create_sale(db, business_a, Decimal("100000.00"), date(2026, 9, 5))

    res = summary(db, business_a, "2026-09")

    assert res["ingresos"] == Decimal("100000.00") and res["emitido"] == Decimal("500000.00")


def test_mixed_history_includes_emitted_and_sales(db, mixed):
    hist = history(db, mixed, "2026-10", months=2)

    assert [(h["month"], h["emitido"], h["ventas"]) for h in hist] == [
        ("2026-09", Decimal("950000.00"), Decimal("350000.00")),
        ("2026-10", Decimal("999999.00"), Decimal("0.00")),
    ]
    assert all(h["ingresos"] is None and h["utilidad"] is None for h in hist)
