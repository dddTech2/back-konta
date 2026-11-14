"""Pruebas del servicio compartido de gastos (Story 11.1): validación, persistencia, perfil y anulación."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.core import expense_service, income_service
from dian_automation.core.expense_service import (
    ExpenseError,
    find_recent_expense_by_number,
    list_month_expenses,
    list_recent_expenses,
    list_recent_expenses_numbered,
    register_expense,
    void_expense,
)
from dian_automation.db.models import (
    Base,
    Business,
    Expense,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    INCOME_SOURCE_MIXED,
    Subscription,
    User,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()

    def _client(suffix: str, status: str, income_source: str = INCOME_SOURCE_MANUAL_SALES):
        user = User(
            id=f"usr-{suffix}", email=f"{suffix}@x.co", full_name=suffix, role="CLIENT",
            telegram_chat_id=1000 + len(suffix), is_telegram_linked=True, is_active=True,
        )
        biz = Business(
            id=f"biz-{suffix}", client_id=user.id, legal_name=suffix, commercial_name=suffix,
            nit="901008579", dv="7", is_active=True, income_source=income_source,
        )
        sub = Subscription(
            id=f"sub-{suffix}", client_id=user.id, plan="TRIMESTRAL",
            discount_rate=Decimal("5.00"), base_price=Decimal("150000.00"),
            final_price=Decimal("142500.00"), start_date=date.today() - timedelta(days=10),
            cutoff_date=date.today() + timedelta(days=80),
            grace_period_end=date.today() + timedelta(days=83), status=status,
        )
        return [user, biz, sub]

    session.add_all(
        _client("ok", "ACTIVO")
        + _client("mixed", "ACTIVO", income_source=INCOME_SOURCE_MIXED)
        + _client("blocked", "BLOQUEADO")
        + _client("dian", "ACTIVO", income_source=INCOME_SOURCE_DIAN)
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _register(db, who="ok", **kwargs):
    kwargs.setdefault("recorded_via", expense_service.RECORDED_VIA_TELEGRAM)
    user = db.get(User, f"usr-{who}")
    business = db.get(Business, f"biz-{who}")
    return register_expense(db, user=user, business=business, **kwargs)


def _count(db) -> int:
    return db.query(Expense).count()


# ==============================================================================
# Registro y validación básica
# ==============================================================================


def test_valid_expense_is_persisted_with_context(db):
    fixed_now = datetime(2026, 11, 14, 15, 0, 0)
    expense = _register(db, total="45000", description="Harina y azúcar", now=fixed_now)

    assert expense.business_id == "biz-ok"
    assert expense.recorded_by_user_id == "usr-ok"
    assert expense.recorded_via == "TELEGRAM"
    assert expense.total_amount == Decimal("45000.00")
    assert isinstance(expense.total_amount, Decimal)
    assert expense.description == "Harina y azúcar"
    assert expense.expense_date == date(2026, 11, 14)
    assert expense.created_at is not None
    assert _count(db) == 1


def test_recorded_via_web_is_accepted(db):
    expense = _register(db, total="12000.50", recorded_via=expense_service.RECORDED_VIA_WEB)
    assert expense.recorded_via == "WEB"
    assert expense.total_amount == Decimal("12000.50")


def test_mixed_business_can_register_expenses(db):
    expense = _register(db, who="mixed", total="30000", description="Empaques")
    assert expense.business_id == "biz-mixed"
    assert expense.total_amount == Decimal("30000.00")


def test_dian_business_cannot_register_expenses(db):
    with pytest.raises(ExpenseError) as exc:
        _register(db, who="dian", total="50000")

    assert exc.value.code == ExpenseError.NOT_MANUAL_SALES
    assert exc.value.message == "Tu perfil no registra gastos"
    assert _count(db) == 0


def test_blocked_business_cannot_register_expenses(db):
    with pytest.raises(ExpenseError) as exc:
        _register(db, who="blocked", total="50000")

    assert exc.value.code == ExpenseError.BUSINESS_BLOCKED
    assert "Suscripción suspendida" in exc.value.message
    assert _count(db) == 0


@pytest.mark.parametrize("raw", ["0", "0.00", "-100", "-0.5"])
def test_zero_or_negative_total_is_rejected(db, raw):
    with pytest.raises(ExpenseError) as exc:
        _register(db, total=raw)

    assert exc.value.code == ExpenseError.NON_POSITIVE_TOTAL
    assert _count(db) == 0


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "abc", "$150000", "150,5", "150.000,5", "150.000", "100.125", "1e5", ".5", "100.", "١٢٣", "1" * 13, None, 1.5],
)
def test_invalid_total_format_is_rejected(db, raw):
    with pytest.raises(ExpenseError) as exc:
        _register(db, total=raw)

    assert exc.value.code == ExpenseError.INVALID_TOTAL
    assert _count(db) == 0


def test_description_too_long_is_rejected(db):
    with pytest.raises(ExpenseError) as exc:
        _register(db, total="1000", description="a" * 501)

    assert exc.value.code == ExpenseError.DESCRIPTION_TOO_LONG
    assert _count(db) == 0


# ==============================================================================
# Regla de fecha en gastos
# ==============================================================================


def test_expense_with_previous_month_date_is_accepted(db):
    fixed_now = datetime(2026, 11, 14, 15, 0, 0)
    expense = _register(db, total="75000", record_date="2026-10-05", now=fixed_now)
    assert expense.expense_date == date(2026, 10, 5)


def test_expense_with_date_out_of_range_is_rejected(db):
    fixed_now = datetime(2026, 11, 14, 15, 0, 0)
    with pytest.raises(ExpenseError) as exc:
        _register(db, total="75000", record_date="2026-09-30", now=fixed_now)

    assert exc.value.code == ExpenseError.DATE_OUT_OF_RANGE
    assert "Solo puedes registrar con fecha de este mes o del mes anterior" in exc.value.message
    assert _count(db) == 0


def test_expense_with_future_date_is_rejected(db):
    fixed_now = datetime(2026, 11, 14, 15, 0, 0)
    with pytest.raises(ExpenseError) as exc:
        _register(db, total="75000", record_date="2026-11-15", now=fixed_now)

    assert exc.value.code == ExpenseError.DATE_OUT_OF_RANGE
    assert _count(db) == 0


def test_expense_with_invalid_date_format_is_rejected(db):
    with pytest.raises(ExpenseError) as exc:
        _register(db, total="75000", record_date="invalida")

    assert exc.value.code == ExpenseError.INVALID_DATE
    assert _count(db) == 0


# ==============================================================================
# Listados y búsqueda numerada
# ==============================================================================


def test_list_month_expenses_filters_and_orders_correctly(db):
    biz = db.get(Business, "biz-ok")
    user = db.get(User, "usr-ok")

    e1 = Expense(
        id="e-1", business_id=biz.id, total_amount=Decimal("10000.00"),
        recorded_via="WEB", recorded_by_user_id=user.id, expense_date=date(2026, 10, 5),
        created_at=datetime(2026, 10, 5, 10, 0, 0),
    )
    e2 = Expense(
        id="e-2", business_id=biz.id, total_amount=Decimal("20000.00"),
        recorded_via="WEB", recorded_by_user_id=user.id, expense_date=date(2026, 10, 15),
        created_at=datetime(2026, 10, 15, 9, 0, 0),
    )
    e3 = Expense(
        id="e-3", business_id=biz.id, total_amount=Decimal("30000.00"),
        recorded_via="WEB", recorded_by_user_id=user.id, expense_date=date(2026, 10, 15),
        created_at=datetime(2026, 10, 15, 14, 0, 0),
    )
    # Gasto en otro mes
    e_nov = Expense(
        id="e-nov", business_id=biz.id, total_amount=Decimal("40000.00"),
        recorded_via="WEB", recorded_by_user_id=user.id, expense_date=date(2026, 11, 1),
        created_at=datetime(2026, 11, 1, 10, 0, 0),
    )
    # Gasto anulado en octubre
    e_void = Expense(
        id="e-void", business_id=biz.id, total_amount=Decimal("50000.00"),
        recorded_via="WEB", recorded_by_user_id=user.id, expense_date=date(2026, 10, 10),
        created_at=datetime(2026, 10, 10, 10, 0, 0), voided_at=datetime(2026, 10, 10, 12, 0, 0),
        voided_by_user_id=user.id,
    )
    db.add_all([e1, e2, e3, e_nov, e_void])
    db.commit()

    results = list_month_expenses(db, biz, "2026-10")
    assert len(results) == 3
    # Orden expense_date desc, created_at desc, id desc: e3 (10-15 14:00), e2 (10-15 09:00), e1 (10-05 10:00)
    assert [e.id for e in results] == ["e-3", "e-2", "e-1"]


def test_list_recent_expenses_numbered_and_find_by_number(db):
    biz = db.get(Business, "biz-ok")
    user = db.get(User, "usr-ok")

    e1 = _register(db, total="1000", description="Primero")
    e2 = _register(db, total="2000", description="Segundo")
    e3 = _register(db, total="3000", description="Tercero")

    numbered = list_recent_expenses_numbered(db, biz, limit=10)
    assert len(numbered) == 3
    # Más reciente a más antiguo
    assert numbered[0][0] == 3 and numbered[0][1].id == e3.id
    assert numbered[1][0] == 2 and numbered[1][1].id == e2.id
    assert numbered[2][0] == 1 and numbered[2][1].id == e1.id

    found2 = find_recent_expense_by_number(db, biz, 2)
    assert found2 is not None
    assert found2.id == e2.id

    assert find_recent_expense_by_number(db, biz, 0) is None
    assert find_recent_expense_by_number(db, biz, 99) is None


# ==============================================================================
# Anulación de gastos
# ==============================================================================


def test_void_expense_success(db):
    expense = _register(db, total="60000", description="Gasto a anular")
    user = db.get(User, "usr-ok")
    biz = db.get(Business, "biz-ok")

    void_time = datetime(2026, 11, 14, 16, 0, 0)
    voided = void_expense(db, user=user, business=biz, expense_id=expense.id, now=void_time)

    assert voided.id == expense.id
    assert voided.voided_at == void_time
    assert voided.voided_by_user_id == user.id

    # La segunda anulación falla con ALREADY_VOIDED
    with pytest.raises(ExpenseError) as exc:
        void_expense(db, user=user, business=biz, expense_id=expense.id)
    assert exc.value.code == ExpenseError.ALREADY_VOIDED
    assert exc.value.message == "Ese gasto ya está anulado."


def test_void_expense_nonexistent_raises_not_found(db):
    user = db.get(User, "usr-ok")
    biz = db.get(Business, "biz-ok")

    with pytest.raises(ExpenseError) as exc:
        void_expense(db, user=user, business=biz, expense_id="no-existe")
    assert exc.value.code == ExpenseError.EXPENSE_NOT_FOUND
    assert exc.value.message == "No encontramos ese gasto."


def test_void_expense_dian_business_rejected(db):
    user = db.get(User, "usr-dian")
    biz = db.get(Business, "biz-dian")

    with pytest.raises(ExpenseError) as exc:
        void_expense(db, user=user, business=biz, expense_id="cualquiera")
    assert exc.value.code == ExpenseError.NOT_MANUAL_SALES
    assert exc.value.message == "Tu perfil no registra gastos"


def test_void_expense_blocked_business_rejected(db):
    user = db.get(User, "usr-blocked")
    biz = db.get(Business, "biz-blocked")

    with pytest.raises(ExpenseError) as exc:
        void_expense(db, user=user, business=biz, expense_id="cualquiera")
    assert exc.value.code == ExpenseError.BUSINESS_BLOCKED
    assert "Suscripción suspendida" in exc.value.message
