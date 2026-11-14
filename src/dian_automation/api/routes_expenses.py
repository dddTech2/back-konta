"""Rutas de gastos del contribuyente (canal web). Adaptador HTTP delgado sobre `core/expense_service.py` (Story 11.1)."""

from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from dian_automation.api.dependencies import get_business_with_access
from dian_automation.api.schemas import (
    ExpenseCreateRequest,
    ExpenseListResponse,
    ExpenseResponse,
    ExpenseVoidResponse,
)
from dian_automation.core import expense_service, income_service
from dian_automation.core.business_profile import has_manual_sales
from dian_automation.db.database import get_db
from dian_automation.db.models import Business, Expense, User

router = APIRouter(prefix="/api/expenses", tags=["Gastos"])

_BOGOTA_TZ = ZoneInfo("America/Bogota")


@router.get("/{business_id}", response_model=ExpenseListResponse)
def list_expenses(
    business_id: str,
    month: Optional[str] = Query(None),
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    db: Session = Depends(get_db),
) -> ExpenseListResponse:
    """Lista los gastos no anulados del mes (por defecto mes actual en Bogotá) para un negocio con ventas manuales o mixto."""
    business, user, _access = business_access

    if not has_manual_sales(business):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Este servicio no aplica a tu tipo de negocio.")

    target_month = month
    if not target_month:
        target_month = datetime.now(_BOGOTA_TZ).strftime("%Y-%m")

    try:
        expenses = expense_service.list_month_expenses(db, business=business, month=target_month)
    except income_service.IncomeError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message) from exc

    return ExpenseListResponse(
        month=target_month,
        expenses=expenses,
    )


@router.post("/{business_id}", response_model=ExpenseResponse, status_code=status.HTTP_201_CREATED)
def create_expense(
    payload: ExpenseCreateRequest,
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    db: Session = Depends(get_db),
) -> Expense:
    """Registra un gasto del dueño autenticado (canal WEB).

    Sesión inválida -> 401; negocio inexistente o ajeno -> 404; perfil DIAN -> 404;
    suscripción bloqueada -> 409; total, descripción o fecha inválidos -> 422.
    """
    business, user, _access = business_access
    try:
        return expense_service.register_expense(
            db,
            user=user,
            business=business,
            total=payload.total_amount,
            description=payload.description,
            recorded_via=expense_service.RECORDED_VIA_WEB,
            record_date=payload.expense_date,
        )
    except expense_service.ExpenseError as exc:
        if exc.code == expense_service.ExpenseError.BUSINESS_BLOCKED:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
        if exc.code == expense_service.ExpenseError.NOT_MANUAL_SALES:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message) from exc


@router.post("/{business_id}/{expense_id}/void", response_model=ExpenseVoidResponse, status_code=status.HTTP_200_OK)
def void_expense(
    business_id: str,
    expense_id: str,
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    db: Session = Depends(get_db),
) -> Expense:
    """Anula un gasto del negocio."""
    business, user, _access = business_access
    try:
        return expense_service.void_expense(
            db,
            user=user,
            business=business,
            expense_id=expense_id,
        )
    except expense_service.ExpenseError as exc:
        if exc.code == expense_service.ExpenseError.BUSINESS_BLOCKED:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
        if exc.code == expense_service.ExpenseError.NOT_MANUAL_SALES:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
        if exc.code == expense_service.ExpenseError.ALREADY_VOIDED:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
        if exc.code == expense_service.ExpenseError.EXPENSE_NOT_FOUND:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message) from exc
