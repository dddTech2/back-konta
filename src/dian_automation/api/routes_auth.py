"""Endpoints de autenticación web: OTP por Telegram -> JWT y estado de sesión."""

from typing import Any, Dict

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from dian_automation.api.dependencies import get_current_user
from dian_automation.api.schemas import (
    MeResponse,
    OTPRequestResponse,
    OTPRequestSchema,
    OTPVerifySchema,
    TokenResponse,
)
from dian_automation.core import auth_service
from dian_automation.db.database import get_db
from dian_automation.db.models import User

router = APIRouter(prefix="/api/auth", tags=["Auth"])


@router.post("/request-otp", response_model=OTPRequestResponse, status_code=status.HTTP_202_ACCEPTED)
def request_otp(payload: OTPRequestSchema, db: Session = Depends(get_db)):
    """Envía un código de 6 dígitos al Telegram vinculado del contribuyente."""
    auth_service.request_otp(db, payload.identifier)
    return OTPRequestResponse()


@router.post("/verify-otp", response_model=TokenResponse)
def verify_otp(payload: OTPVerifySchema, db: Session = Depends(get_db)):
    """Canjea el código por un JWT Bearer."""
    return TokenResponse(access_token=auth_service.verify_otp(db, payload.identifier, payload.code))


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Negocio activo y estado de suscripción de la sesión; nunca responde 403."""
    return auth_service.get_me(user, db)
