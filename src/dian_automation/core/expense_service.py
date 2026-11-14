"""Servicio de dominio compartido para registrar gastos (Telegram y Web) (Story 11.1).

Concentra la validación del total, la descripción y la fecha, y la persistencia de `Expense`.
Reutiliza las reglas de validación de `sales_service` y traduce las excepciones a `ExpenseError`.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, List, Optional, Tuple, Union

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from dian_automation.core import income_service, sales_service
from dian_automation.core.business_profile import has_manual_sales
from dian_automation.db.models import Business, Expense, User
from dian_automation.subscriptions.lockout_service import SubscriptionLockoutService

RECORDED_VIA_TELEGRAM = "TELEGRAM"
RECORDED_VIA_WEB = "WEB"


class ExpenseError(Exception):
    """Error tipado del registro de gastos; `code` permite a cada canal elegir su mensaje."""

    INVALID_TOTAL = "INVALID_TOTAL"
    NON_POSITIVE_TOTAL = "NON_POSITIVE_TOTAL"
    DESCRIPTION_TOO_LONG = "DESCRIPTION_TOO_LONG"
    BUSINESS_BLOCKED = "BUSINESS_BLOCKED"
    NOT_MANUAL_SALES = "NOT_MANUAL_SALES"
    EXPENSE_NOT_FOUND = "EXPENSE_NOT_FOUND"
    ALREADY_VOIDED = "ALREADY_VOIDED"
    DATE_OUT_OF_RANGE = "DATE_OUT_OF_RANGE"
    INVALID_DATE = "INVALID_DATE"

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def register_expense(
    db: Session,
    *,
    user: User,
    business: Business,
    total: Union[str, int, Decimal, None],
    description: Optional[str] = None,
    recorded_via: str,
    record_date: Optional[Union[date, str]] = None,
    now: Optional[datetime] = None,
) -> Expense:
    """Valida y persiste un gasto del cliente autenticado.

    `user` y `business` los entrega el guardia de cada canal (nunca ids arbitrarios). Lanza
    `ExpenseError` si el total, la descripción o la fecha no son válidos, si la suscripción está
    BLOQUEADA, o si el negocio no tiene ventas manuales o perfil mixto.
    """
    if SubscriptionLockoutService.is_client_blocked(db, user.id):
        raise ExpenseError(
            ExpenseError.BUSINESS_BLOCKED,
            "Suscripción suspendida por pago pendiente: no se pueden registrar gastos.",
        )

    if not has_manual_sales(business):
        raise ExpenseError(
            ExpenseError.NOT_MANUAL_SALES,
            "Tu perfil no registra gastos",
        )

    try:
        total_amount = sales_service.parse_total(total)
    except sales_service.SalesError as exc:
        raise ExpenseError(exc.code, exc.message) from exc

    try:
        clean_description = sales_service.normalize_description(description)
    except sales_service.SalesError as exc:
        raise ExpenseError(exc.code, exc.message) from exc

    if now is None:
        now = datetime.utcnow()

    try:
        expense_date = sales_service.resolve_record_date(record_date, now=now)
    except sales_service.SalesError as exc:
        raise ExpenseError(exc.code, exc.message) from exc

    expense = Expense(
        business_id=business.id,
        total_amount=total_amount,
        description=clean_description,
        recorded_via=recorded_via,
        recorded_by_user_id=user.id,
        expense_date=expense_date,
        created_at=now,
    )
    db.add(expense)
    db.commit()
    db.refresh(expense)
    return expense


def list_recent_expenses(db: Session, business: Business, limit: int = 10) -> List[Expense]:
    """Últimos `limit` gastos NO anulados del negocio, del más reciente al más antiguo (created_at desc, id desc)."""
    return (
        db.query(Expense)
        .filter(
            Expense.business_id == business.id,
            Expense.voided_at.is_(None),
        )
        .order_by(Expense.created_at.desc(), Expense.id.desc())
        .limit(limit)
        .all()
    )


def list_recent_expenses_numbered(
    db: Session, business: Business, limit: int = 10
) -> List[Tuple[int, Expense]]:
    """Últimos `limit` gastos NO anulados, de más reciente a más antiguo, cada uno con su número estable.

    El número de un gasto es su posición 1-based entre TODOS los gastos del negocio (anulados incluidos)
    ordenados por (created_at, id): no cambia al registrar ni anular otros gastos.
    """
    recent = list_recent_expenses(db, business, limit=limit)
    numbered = []
    for e in recent:
        count = (
            db.query(func.count(Expense.id))
            .filter(
                Expense.business_id == business.id,
                or_(
                    Expense.created_at < e.created_at,
                    and_(Expense.created_at == e.created_at, Expense.id <= e.id),
                ),
            )
            .scalar()
        )
        numbered.append((int(count or 0), e))
    return numbered


def find_recent_expense_by_number(
    db: Session, business: Business, number: int, limit: int = 10
) -> Optional[Expense]:
    """El gasto con ese número estable SOLO si está entre los últimos `limit` no anulados; None en otro caso."""
    if number <= 0:
        return None
    for num, expense in list_recent_expenses_numbered(db, business, limit=limit):
        if num == number:
            return expense
    return None


def list_month_expenses(
    db: Session, business: Business, month: str, limit: int = 100
) -> List[Expense]:
    """Gastos NO anulados del negocio con expense_date dentro del mes 'YYYY-MM', orden expense_date desc, created_at desc, id desc.

    Mes inválido -> lanza income_service.IncomeError(INVALID_MONTH).
    """
    year, month_num = income_service._parse_month(month)
    start_date, end_date, _, _ = income_service._month_range(year, month_num)
    return (
        db.query(Expense)
        .filter(
            Expense.business_id == business.id,
            Expense.expense_date >= start_date,
            Expense.expense_date < end_date,
            Expense.voided_at.is_(None),
        )
        .order_by(Expense.expense_date.desc(), Expense.created_at.desc(), Expense.id.desc())
        .limit(limit)
        .all()
    )


def void_expense(
    db: Session,
    *,
    user: User,
    business: Business,
    expense_id: str,
    now: Optional[datetime] = None,
) -> Expense:
    """Anula un gasto del negocio. `now` es UTC naive (por defecto datetime.utcnow())."""
    if SubscriptionLockoutService.is_client_blocked(db, user.id):
        raise ExpenseError(
            ExpenseError.BUSINESS_BLOCKED,
            "Suscripción suspendida por pago pendiente: no se pueden anular gastos.",
        )

    if not has_manual_sales(business):
        raise ExpenseError(
            ExpenseError.NOT_MANUAL_SALES,
            "Tu perfil no registra gastos",
        )

    expense = (
        db.query(Expense)
        .filter(Expense.id == expense_id, Expense.business_id == business.id)
        .first()
    )
    if not expense:
        raise ExpenseError(ExpenseError.EXPENSE_NOT_FOUND, "No encontramos ese gasto.")

    if expense.voided_at is not None:
        raise ExpenseError(ExpenseError.ALREADY_VOIDED, "Ese gasto ya está anulado.")

    if now is None:
        now = datetime.utcnow()

    updated = (
        db.query(Expense)
        .filter(
            Expense.id == expense_id,
            Expense.business_id == business.id,
            Expense.voided_at.is_(None),
        )
        .update(
            {"voided_at": now, "voided_by_user_id": user.id},
            synchronize_session=False,
        )
    )
    if updated == 0:
        db.rollback()
        raise ExpenseError(ExpenseError.ALREADY_VOIDED, "Ese gasto ya está anulado.")

    db.commit()
    db.refresh(expense)
    return expense
