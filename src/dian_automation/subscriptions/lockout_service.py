"""Servicio de Bloqueo Dual de Acceso en Web y Telegram al Vencer la Gracia.

Garantiza la suspensión simultánea de servicios al expirar las 72 horas de gracia:
- En Web/API: Rechazo con código 403 Forbidden y redirección a /servicio-suspendido.
- En Telegram: Interceptación estricta de consultas con aviso de pago pendiente sin exponer datos fiscales.
"""

import logging
from datetime import date, datetime
from typing import Optional, Dict, Any, List, Callable
from sqlalchemy.orm import Session

from dian_automation.db.models import User, Subscription

logger = logging.getLogger("lockout_service")


class SubscriptionBlockedError(Exception):
    """Excepción lanzada cuando un usuario bloqueado intenta acceder a rutas protegidas de la API/Web."""

    def __init__(
        self,
        message: str = "Tu suscripción se encuentra suspendida temporalmente por pago pendiente. Comunícate con Katerinn para reactivar tus reportes.",
        status_code: int = 403,
        redirect_url: str = "/servicio-suspendido",
        error_code: str = "SUBSCRIPTION_BLOCKED",
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.redirect_url = redirect_url
        self.error_code = error_code

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status_code": self.status_code,
            "error": self.error_code,
            "message": self.message,
            "redirect_url": self.redirect_url,
        }


class SubscriptionLockoutService:
    """Controlador centralizado de bloqueo dual para Web y Telegram."""

    BLOCKED_TELEGRAM_MESSAGE = (
        "⚠️ *Servicio Suspendido*\n\n"
        "Tu suscripción se encuentra suspendida temporalmente por pago pendiente. "
        "Comunícate con Katerinn para reactivar tus reportes."
    )

    @classmethod
    def verify_user_web_access(
        cls, user_id: str, db: Session, reference_date: Optional[date] = None
    ) -> Dict[str, Any]:
        """Verifica si el usuario tiene permitido el acceso a la plataforma Web/API."""
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return {
                "allowed": False,
                "status_code": 404,
                "error_code": "USER_NOT_FOUND",
                "message": "Usuario no encontrado.",
                "redirect_url": "/login",
            }

        # Administradores y soporte técnico tienen acceso irrestricto
        if user.role in ("ADMIN", "TECH_OPS"):
            return {
                "allowed": True,
                "status_code": 200,
                "role": user.role,
                "has_warning_banner": False,
                "redirect_url": None,
                "message": "Acceso administrativo concedido.",
            }

        # Consultar suscripción más reciente del cliente
        sub = (
            db.query(Subscription)
            .filter(Subscription.client_id == user.id)
            .order_by(Subscription.created_at.desc())
            .first()
        )

        if not sub:
            return {
                "allowed": True,
                "status_code": 200,
                "has_warning_banner": False,
                "redirect_url": None,
                "message": "Usuario sin suscripción activa asociada.",
            }

        today = reference_date or date.today()

        # Si el estado explícito es BLOQUEADO o la gracia ya expiró
        if sub.status == "BLOQUEADO" or (sub.grace_period_end < today and sub.status != "ACTIVO"):
            return {
                "allowed": False,
                "status_code": 403,
                "error_code": "SUBSCRIPTION_BLOCKED",
                "redirect_url": "/servicio-suspendido",
                "subscription_id": sub.id,
                "plan": sub.plan,
                "amount_due": float(sub.final_price),
                "message": (
                    "Tu suscripción se encuentra suspendida temporalmente por pago pendiente. "
                    "Comunícate con Katerinn para reactivar el acceso web."
                ),
            }

        # Si está en mora pero dentro de la ventana de gracia
        if sub.status == "EN_MORA":
            days_left = max(0, (sub.grace_period_end - today).days)
            return {
                "allowed": True,
                "status_code": 200,
                "has_warning_banner": True,
                "days_left_in_grace": days_left,
                "redirect_url": None,
                "message": f"Acceso web permitido en periodo de gracia. Restan {days_left} días de gracia.",
            }

        return {
            "allowed": True,
            "status_code": 200,
            "has_warning_banner": False,
            "redirect_url": None,
            "message": "Acceso regular permitido.",
        }

    @classmethod
    def enforce_web_access(
        cls, user: User, db: Session, reference_date: Optional[date] = None
    ) -> None:
        """Lanza SubscriptionBlockedError (403 Forbidden) si el acceso web no está permitido."""
        check = cls.verify_user_web_access(user.id, db, reference_date=reference_date)
        if not check["allowed"]:
            raise SubscriptionBlockedError(
                message=check["message"],
                status_code=check["status_code"],
                redirect_url=check.get("redirect_url", "/servicio-suspendido"),
            )

    @classmethod
    def process_expired_grace_lockouts(
        cls,
        db: Session,
        execution_date: Optional[date] = None,
        telegram_sender: Optional[Callable[[int, str], bool]] = None,
    ) -> Dict[str, Any]:
        """Transiciona a BLOQUEADO todas las suscripciones cuya fecha de gracia haya vencido sin pago."""
        today = execution_date or date.today()

        # Suscripciones en mora o activas cuyo periodo de gracia ya expiró (grace_period_end < today)
        expired_subs = (
            db.query(Subscription)
            .filter(
                Subscription.status.in_(["ACTIVO", "EN_MORA"]),
                Subscription.grace_period_end < today,
            )
            .all()
        )

        locked_out_count = 0
        notifications_sent = 0
        details: List[Dict[str, Any]] = []

        for sub in expired_subs:
            user = sub.client
            if not user:
                continue

            sub.status = "BLOQUEADO"
            sub.updated_at = datetime.combine(today, datetime.now().time())
            locked_out_count += 1

            biz_name = (
                user.businesses[0].commercial_name if user.businesses else "tu empresa"
            )

            # Notificación de suspensión formal por Telegram
            msg = (
                "🚫 *Servicio Suspendido por Pago Pendiente*\n\n"
                f"Hola *{user.full_name}*, el periodo de gracia de 72 horas para tu plan *{sub.plan}* "
                f"(*{biz_name}*) ha finalizado sin registrar la renovación.\n\n"
                "Tu suscripción se encuentra suspendida temporalmente por pago pendiente. "
                "Comunícate con Katerinn para reactivar tus reportes."
            )

            sent = False
            if user.is_telegram_linked and user.telegram_chat_id:
                if telegram_sender:
                    try:
                        telegram_sender(user.telegram_chat_id, msg)
                        sent = True
                    except Exception as e:
                        logger.error(f"Error enviando aviso de bloqueo a {user.telegram_chat_id}: {e}")
                else:
                    sent = True

                if sent:
                    notifications_sent += 1

            logger.warning(
                f"Suscripción {sub.id} bloqueada por vencimiento de gracia para {user.email}. "
                f"Acceso Web y Telegram deshabilitados."
            )

            details.append({
                "subscription_id": sub.id,
                "client_id": user.id,
                "client_email": user.email,
                "status": "BLOQUEADO",
                "notification_sent": sent,
            })

        db.commit()

        return {
            "execution_date": today.isoformat(),
            "locked_out_count": locked_out_count,
            "notifications_sent": notifications_sent,
            "details": details,
        }
