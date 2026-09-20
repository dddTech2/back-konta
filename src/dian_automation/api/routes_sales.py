"""Rutas de ventas del contribuyente (canal web). Adaptador HTTP delgado sobre `core/sales_service.py`."""

from typing import Any, Dict, Tuple

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from dian_automation.api.dependencies import get_business_with_access
from dian_automation.api.schemas import SaleCreateRequest, SaleResponse
from dian_automation.core import sales_service
from dian_automation.db.database import get_db
from dian_automation.db.models import Business, Sale, User
from dian_automation.subscriptions.lockout_service import SubscriptionBlockedError

router = APIRouter(prefix="/api/sales", tags=["Ventas"])


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
        raise HTTPException(status_code=422, detail=exc.message) from exc
