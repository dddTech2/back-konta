"""Rutas de ventas del contribuyente (canal web). Adaptador HTTP delgado sobre `core/sales_service.py`."""

from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from dian_automation.api.dependencies import get_business_with_access
from dian_automation.api.schemas import (
    SaleCreateRequest,
    SaleListResponse,
    SaleResponse,
    SaleVoidResponse,
)
from dian_automation.core import income_service, sales_service
from dian_automation.db.database import get_db
from dian_automation.db.models import Business, INCOME_SOURCE_MANUAL_SALES, Sale, User
from dian_automation.subscriptions.lockout_service import SubscriptionBlockedError

router = APIRouter(prefix="/api/sales", tags=["Ventas"])

_BOGOTA_TZ = ZoneInfo("America/Bogota")


@router.get("/{business_id}", response_model=SaleListResponse)
def list_sales(
    business_id: str,
    month: Optional[str] = Query(None),
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    db: Session = Depends(get_db),
) -> SaleListResponse:
    """Lista las ventas no anuladas del mes (por defecto mes actual en Bogotá) para un negocio MANUAL_SALES."""
    business, user, _access = business_access

    if business.income_source != INCOME_SOURCE_MANUAL_SALES:
        raise HTTPException(status_code=404, detail="Este servicio no aplica a tu tipo de negocio.")

    target_month = month
    if not target_month:
        target_month = datetime.now(_BOGOTA_TZ).strftime("%Y-%m")

    try:
        sales = sales_service.list_month_sales(db, business=business, month=target_month)
    except income_service.IncomeError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc

    return SaleListResponse(
        month=target_month,
        sales=sales,
    )


@router.post("/{business_id}", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
def create_sale(
    payload: SaleCreateRequest,
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    db: Session = Depends(get_db),
) -> Sale:
    """Registra una venta del dueño autenticado (canal WEB).

    Sesión inválida -> 401; negocio inexistente o ajeno -> 404; suscripción BLOQUEADA -> 403
    (manejador global); total o descripción inválidos -> 422.
    """
    business, user, _access = business_access
    try:
        return sales_service.register_sale(
            db,
            user=user,
            business=business,
            total=payload.total_amount,
            description=payload.description,
            recorded_via=sales_service.RECORDED_VIA_WEB,
        )
    except sales_service.SalesError as exc:
        if exc.code == sales_service.SalesError.BUSINESS_BLOCKED:
            # Bloqueo ocurrido entre el guardia de acceso y el servicio: mismo 403 que el resto de rutas.
            raise SubscriptionBlockedError(message=exc.message) from exc
        if exc.code == sales_service.SalesError.NOT_MANUAL_SALES:
            raise HTTPException(status_code=409, detail=exc.message) from exc
        raise HTTPException(status_code=422, detail=exc.message) from exc


@router.post("/{business_id}/{sale_id}/void", response_model=SaleVoidResponse, status_code=status.HTTP_200_OK)
def void_sale(
    business_id: str,
    sale_id: str,
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    db: Session = Depends(get_db),
) -> Sale:
    """Anula una venta del negocio."""
    business, user, _access = business_access
    try:
        return sales_service.void_sale(
            db,
            user=user,
            business=business,
            sale_id=sale_id,
        )
    except sales_service.SalesError as exc:
        if exc.code == sales_service.SalesError.BUSINESS_BLOCKED:
            raise SubscriptionBlockedError(message=exc.message) from exc
        if exc.code == sales_service.SalesError.NOT_MANUAL_SALES:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
        if exc.code == sales_service.SalesError.ALREADY_VOIDED:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
        if exc.code == sales_service.SalesError.SALE_NOT_FOUND:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message) from exc
