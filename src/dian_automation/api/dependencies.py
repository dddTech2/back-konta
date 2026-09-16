"""Dependencias de FastAPI para Kontable."""

from typing import Dict, Any, Tuple
from fastapi import Depends, HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from dian_automation.db.database import get_db
from dian_automation.db.models import Business, User
from dian_automation.subscriptions.lockout_service import (
    SubscriptionLockoutService,
    SubscriptionBlockedError,
)


def get_business_with_access(
    business_id: str,
    db: Session = Depends(get_db),
) -> Tuple[Business, User, Dict[str, Any]]:
    """Valida la existencia del negocio y verifica el estado de la suscripción.
    
    Busca por ID de negocio o por NIT.
    Si el negocio no existe -> 404 Not Found.
    Si la suscripción está bloqueada -> SubscriptionBlockedError (403 Forbidden).
    """
    clean_id = "".join(filter(str.isdigit, business_id)) if business_id else ""
    filters = [Business.id == business_id, Business.nit == business_id]
    if clean_id:
        filters.append(Business.nit == clean_id)

    business = db.query(Business).filter(or_(*filters)).first()
    if not business:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No se encontró el negocio con identificador '{business_id}'.",
        )

    user = business.client
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El negocio no tiene un usuario propietario asociado.",
        )

    # Verificar acceso y estado de suscripción
    access_check = SubscriptionLockoutService.verify_user_web_access(user.id, db)
    if not access_check["allowed"]:
        raise SubscriptionBlockedError(
            message=access_check["message"],
            status_code=access_check["status_code"],
            redirect_url=access_check.get("redirect_url", "/servicio-suspendido"),
            error_code=access_check.get("error_code", "SUBSCRIPTION_BLOCKED"),
        )

    return business, user, access_check
