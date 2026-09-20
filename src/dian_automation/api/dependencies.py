"""Dependencias de FastAPI para Kontable."""

from typing import Dict, Any, Optional, Tuple
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import or_
from sqlalchemy.orm import Session

from dian_automation.core import auth_service
from dian_automation.db.database import get_db
from dian_automation.db.models import Business, User
from dian_automation.subscriptions.lockout_service import (
    SubscriptionLockoutService,
    SubscriptionBlockedError,
)

_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Usuario de la sesión a partir del JWT Bearer; sin token, inválido o expirado -> 401."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise auth_service.AuthServiceError(401, auth_service.INVALID_SESSION_DETAIL)
    return auth_service.get_user_from_token(db, credentials.credentials)


def get_business_with_access(
    business_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Tuple[Business, User, Dict[str, Any]]:
    """Valida la sesión, que el negocio pertenezca al usuario y el estado de su suscripción.

    Busca por ID de negocio o por NIT, solo entre los negocios del usuario autenticado.
    Sin sesión válida -> 401. Negocio inexistente o ajeno -> 404 (mismo mensaje).
    Si la suscripción está bloqueada -> SubscriptionBlockedError (403 Forbidden).
    """
    clean_id = "".join(filter(str.isdigit, business_id)) if business_id else ""
    filters = [Business.id == business_id, Business.nit == business_id]
    if clean_id:
        filters.append(Business.nit == clean_id)

    business = (
        db.query(Business)
        .filter(Business.client_id == user.id, or_(*filters))
        .first()
    )
    if not business:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No se encontró el negocio con identificador '{business_id}'.",
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
