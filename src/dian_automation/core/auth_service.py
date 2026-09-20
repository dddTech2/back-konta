"""Servicio de autenticación web: OTP por Telegram -> JWT Bearer.

Único lugar con lógica de OTP y JWT. Lanza `AuthServiceError` (con `status_code`), que el handler
global de la API traduce a `{"detail": ...}`. El código OTP solo existe en memoria y en el mensaje
de Telegram: en base de datos se guarda su hash HMAC-SHA256 firmado con `JWT_SECRET`.
"""

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import jwt
from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from dian_automation.config import config
from dian_automation.db.models import Business, OTPCode, Subscription, User
from dian_automation.subscriptions.lockout_service import SubscriptionLockoutService
from dian_automation.telegram.client_bot import ClientTelegramBot

logger = logging.getLogger("auth_service")

OTP_LENGTH = 6
OTP_TTL = timedelta(minutes=5)
OTP_RATE_WINDOW = timedelta(minutes=10)
OTP_MAX_REQUESTS = 3
OTP_MAX_ATTEMPTS = 5

INVALID_CODE_DETAIL = "Código inválido o expirado."
INVALID_SESSION_DETAIL = "Sesión inválida o expirada."


class AuthServiceError(Exception):
    """Error de autenticación con el código HTTP que debe devolver la API."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _utcnow() -> datetime:
    """Reloj UTC ingenuo (como `datetime.utcnow` de los modelos); punto único para las pruebas."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _epoch(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def _digits(value: Optional[str]) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit() and ch.isascii())


def _secret() -> bytes:
    if not config.jwt_secret:
        logger.error("JWT_SECRET no está configurado: autenticación web deshabilitada.")
        raise AuthServiceError(500, "Autenticación no configurada en el servidor.")
    return config.jwt_secret.encode("utf-8")


def _hash_code(user_id: str, code: str) -> str:
    return hmac.new(_secret(), f"{user_id}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


MAX_IDENTIFIER_LENGTH = 64
MAX_CODE_LENGTH = 64


def _phone_key(digits: str) -> str:
    """Teléfono comparable: sin el prefijo de país 57 cuando el número trae 12 dígitos."""
    return digits[2:] if len(digits) == 12 and digits.startswith("57") else digits


def resolve_client_by_identifier(db: Session, identifier: str) -> Optional[User]:
    """Usuario CLIENT activo cuyo teléfono o NIT (normalizados a dígitos) coincide.

    El teléfono se compara sin prefijo 57 y el NIT con o sin dígito de verificación. Devuelve None
    si no hay coincidencia o si hay más de un usuario distinto.
    """
    if not identifier or len(identifier) > MAX_IDENTIFIER_LENGTH:
        return None
    digits = _digits(identifier)
    if not digits:
        return None

    clients: List[User] = (
        db.query(User).filter(User.role == "CLIENT", User.is_active.is_(True)).all()
    )
    phone_key = _phone_key(digits)
    matches = {u.id: u for u in clients if u.phone and _phone_key(_digits(u.phone)) == phone_key}

    owners = (
        db.query(User, Business)
        .join(Business, Business.client_id == User.id)
        .filter(User.role == "CLIENT", User.is_active.is_(True))
        .filter(or_(Business.nit == digits, (Business.nit + Business.dv) == digits))
        .all()
    )
    matches.update({user.id: user for user, _business in owners})

    return next(iter(matches.values())) if len(matches) == 1 else None


def request_otp(db: Session, identifier: str) -> None:
    """Genera un OTP, lo envía por Telegram y deja vigente solo el más reciente."""
    _secret()  # falla antes de tocar la base si falta la configuración

    user = resolve_client_by_identifier(db, identifier)
    if user is None:
        raise AuthServiceError(404, "No encontramos una cuenta con ese identificador.")
    if not user.is_telegram_linked or not user.telegram_chat_id:
        raise AuthServiceError(409, "Tu cuenta aún no tiene Telegram vinculado.")

    now = _utcnow()
    recent = (
        db.query(OTPCode)
        .filter(OTPCode.user_id == user.id, OTPCode.created_at > now - OTP_RATE_WINDOW)
        .count()
    )
    if recent >= OTP_MAX_REQUESTS:
        raise AuthServiceError(429, "Demasiadas solicitudes. Intenta de nuevo en unos minutos.")

    code = "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))
    otp = OTPCode(
        user_id=user.id,
        code_hash=_hash_code(user.id, code),
        expires_at=now + OTP_TTL,
        created_at=now,
    )
    db.add(otp)
    db.commit()

    try:
        sent = ClientTelegramBot.send_otp(user.telegram_chat_id, code)
    except Exception:  # noqa: BLE001 - cualquier fallo de envío se trata igual
        sent = False

    if not sent:
        db.delete(otp)
        db.commit()
        raise AuthServiceError(502, "No pudimos enviar el código por Telegram. Intenta de nuevo.")

    db.execute(
        update(OTPCode)
        .where(OTPCode.user_id == user.id, OTPCode.id != otp.id, OTPCode.is_used.is_(False))
        .values(is_used=True)
    )
    db.commit()


def verify_otp(db: Session, identifier: str, code: str) -> str:
    """Canjea un OTP vigente por un JWT. Cualquier fallo responde 401 con el mismo mensaje."""
    _secret()
    invalid = AuthServiceError(401, INVALID_CODE_DETAIL)

    user = resolve_client_by_identifier(db, identifier)
    if user is None:
        raise invalid

    otp = (
        db.query(OTPCode)
        .filter(OTPCode.user_id == user.id, OTPCode.is_used.is_(False))
        .order_by(OTPCode.created_at.desc())
        .first()
    )
    if otp is None or otp.expires_at <= _utcnow() or otp.attempts >= OTP_MAX_ATTEMPTS:
        raise invalid

    # Reserva atómica del intento ANTES de comparar: ráfagas en paralelo no superan el máximo.
    reserved = db.execute(
        update(OTPCode)
        .where(OTPCode.id == otp.id, OTPCode.is_used.is_(False), OTPCode.attempts < OTP_MAX_ATTEMPTS)
        .values(attempts=OTPCode.attempts + 1)
        .returning(OTPCode.attempts)
    ).scalar_one_or_none()
    db.commit()
    if reserved is None:
        raise invalid

    candidate = (code or "").strip()[:MAX_CODE_LENGTH]
    if not hmac.compare_digest(otp.code_hash, _hash_code(user.id, candidate)):
        if reserved >= OTP_MAX_ATTEMPTS:
            db.execute(update(OTPCode).where(OTPCode.id == otp.id).values(is_used=True))
            db.commit()
        raise invalid

    redeemed = db.execute(
        update(OTPCode)
        .where(OTPCode.id == otp.id, OTPCode.is_used.is_(False), OTPCode.expires_at > _utcnow())
        .values(is_used=True)
    )
    db.commit()
    if redeemed.rowcount != 1:
        raise invalid

    return create_access_token(user)


def create_access_token(user: User) -> str:
    now = _utcnow()
    payload = {
        "sub": user.id,
        "iat": _epoch(now),
        "exp": _epoch(now + timedelta(minutes=config.jwt_ttl_minutes)),
    }
    return jwt.encode(payload, _secret(), algorithm=config.jwt_algorithm)


def get_user_from_token(db: Session, token: str) -> User:
    """Valida el JWT (firma, algoritmo y expiración con el reloj del servicio) y devuelve el usuario
    activo; cualquier problema es 401."""
    invalid = AuthServiceError(401, INVALID_SESSION_DETAIL)
    try:
        claims = jwt.decode(
            token,
            _secret(),
            algorithms=[config.jwt_algorithm],
            options={"verify_exp": False, "verify_iat": False, "require": ["sub", "exp"]},
        )
        expires_at = datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc).replace(tzinfo=None)
    except (jwt.PyJWTError, ValueError, TypeError, OverflowError, OSError):
        raise invalid

    if expires_at <= _utcnow():
        raise invalid

    subject = claims["sub"]
    user = db.get(User, subject) if isinstance(subject, str) else None
    if user is None or not user.is_active:
        raise invalid
    return user


def get_me(user: User, db: Session) -> Dict[str, Any]:
    """Estado de sesión para la SPA: negocio activo más antiguo y visibilidad; sin datos fiscales."""
    business = (
        db.query(Business)
        .filter(Business.client_id == user.id, Business.is_active.is_(True))
        .order_by(Business.created_at.asc(), Business.id.asc())
        .first()
    )
    subscription = (
        db.query(Subscription)
        .filter(Subscription.client_id == user.id)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    access = SubscriptionLockoutService.verify_user_web_access(user.id, db)
    return {
        "business_id": business.id if business else None,
        "income_source": business.income_source if business else None,
        "is_provisioned": business is not None,
        "is_blocked": not access["allowed"],
        "subscription_status": subscription.status if subscription else None,
        "has_warning_banner": bool(access.get("has_warning_banner", False)),
        "redirect_url": access.get("redirect_url"),
    }
