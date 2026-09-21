"""Servicio de Deep Linking y vinculación de clientes con Telegram.

Genera enlaces mágicos de un solo uso (https://t.me/<bot>?start=<token>)
y procesa el evento /start para asociar el telegram_chat_id del cliente de forma
atómica y personalizada.
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, Callable
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from dian_automation.branding import BRAND_NAME, BRAND_TAGLINE
from dian_automation.config import config
from dian_automation.db.models import User, Business, TelegramLinkToken

logger = logging.getLogger("deep_linking")


class TelegramDeepLinkingService:
    """Gestiona la emisión y validación de tokens de vinculación por Deep Linking."""

    DEFAULT_EXPIRATION_HOURS = 72

    @classmethod
    def generate_link_token(
        cls,
        user_id: str,
        db: Session,
        expires_in_hours: int = DEFAULT_EXPIRATION_HOURS,
        bot_username: str = "KontaBot",
    ) -> Tuple[TelegramLinkToken, str]:
        """Genera un token seguro de un solo uso y su enlace correspondiente."""
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError(f"Usuario con ID '{user_id}' no encontrado")

        token_str = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(hours=expires_in_hours)

        token_record = TelegramLinkToken(
            user_id=user.id,
            token=token_str,
            is_used=False,
            expires_at=expires_at,
        )
        db.add(token_record)
        db.commit()
        db.refresh(token_record)

        deep_link_url = f"https://t.me/{bot_username}?start={token_str}"
        logger.info(f"Token de vinculación generado para {user.email} (expira en {expires_in_hours}h)")

        return token_record, deep_link_url

    @classmethod
    def format_welcome_message(cls, user: User, business: Optional[Business] = None) -> str:
        """Construye el mensaje de bienvenida personalizado en Markdown para el cliente."""
        biz_name = business.commercial_name if business and business.commercial_name else (business.legal_name if business else "tu negocio")
        # Se muestra el NIT tal como fue registrado, sin el dígito de verificación
        # (ese DV es calculado internamente por Konta, nunca lo suministra el cliente).
        nit_str = business.nit if business else "N/A"
        dashboard_link = f"{config.kontable_web_url}?nit={business.nit}" if business else None

        message = (
            f"👋 ¡Hola, *{user.full_name}*! Bienvenido a *{BRAND_NAME}*.\n"
            f"_{BRAND_TAGLINE}_\n\n"
            "🏢 Tu cuenta ha sido vinculada exitosamente con tu empresa:\n"
            f"*{biz_name}* (NIT: `{nit_str}`)\n\n"
            "A partir de ahora recibirás en este chat:\n"
            "📊 Resúmenes mensuales de facturación e IVA\n"
            "📅 Recordatorios de vencimientos tributarios DIAN\n"
            "🔔 Alertas y estados de tus descargas automáticas\n\n"
            "💡 _Escribe /resumen para consultar tu balance fiscal actual._"
        )
        if dashboard_link:
            message += f"\n📱 _O visualiza tu dashboard completo aquí:_\n{dashboard_link}"
        return message

    @classmethod
    def process_start_payload(
        cls,
        chat_id: int,
        payload: str,
        db: Session,
        telegram_username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Valida el token recibido en /start <token> y vincula el chat_id del usuario."""
        if not payload or not payload.strip():
            return {
                "success": False,
                "reason": "EMPTY_TOKEN",
                "message": "❌ No se proporcionó ningún token de vinculación en el comando.",
            }

        token_clean = payload.strip()
        record = (
            db.query(TelegramLinkToken)
            .filter(TelegramLinkToken.token == token_clean)
            .first()
        )

        if not record:
            return {
                "success": False,
                "reason": "TOKEN_NOT_FOUND",
                "message": "❌ El enlace de vinculación no es válido o ha expirado.",
            }

        if record.is_used:
            return {
                "success": False,
                "reason": "TOKEN_ALREADY_USED",
                "message": "⚠️ Este enlace de vinculación ya fue utilizado anteriormente.",
            }

        now = datetime.utcnow()
        if record.expires_at < now:
            return {
                "success": False,
                "reason": "TOKEN_EXPIRED",
                "message": "⌛ El enlace de vinculación ha expirado (vigencia de 72 horas). Solicita uno nuevo a tu administrador.",
            }

        user = record.user

        # Verificar si otro usuario ya tiene asignado este telegram_chat_id
        conflicting_user = (
            db.query(User)
            .filter(User.telegram_chat_id == chat_id, User.id != user.id)
            .first()
        )
        if conflicting_user:
            logger.warning(
                f"Conflicto de vinculación: chat_id={chat_id} ya asignado a user_id={conflicting_user.id}, "
                f"intento desde user_id={user.id}"
            )
            return {
                "success": False,
                "reason": "CHAT_ALREADY_LINKED",
                "message": (
                    f"⚠️ Este Telegram ya está asociado a otra cuenta de {BRAND_NAME}. "
                    "Pide a tu administradora que lo libere y vuelve a abrir tu enlace."
                ),
            }

        # Vincular usuario y consumir token
        user.telegram_chat_id = chat_id
        user.telegram_username = telegram_username
        user.is_telegram_linked = True

        record.is_used = True
        record.used_at = now

        try:
            db.commit()
            db.refresh(user)
        except IntegrityError:
            db.rollback()
            logger.warning(
                f"IntegrityError al vincular chat_id={chat_id} a user_id={user.id}"
            )
            return {
                "success": False,
                "reason": "CHAT_ALREADY_LINKED",
                "message": (
                    f"⚠️ Este Telegram ya está asociado a otra cuenta de {BRAND_NAME}. "
                    "Pide a tu administradora que lo libere y vuelve a abrir tu enlace."
                ),
            }

        # Obtener negocio principal
        primary_biz = user.businesses[0] if user.businesses else None
        welcome_msg = cls.format_welcome_message(user, primary_biz)

        logger.info(f"Usuario {user.email} vinculado exitosamente a chat_id={chat_id}")

        return {
            "success": True,
            "user_id": user.id,
            "user_email": user.email,
            "chat_id": chat_id,
            "welcome_message": welcome_msg,
            "business_name": primary_biz.commercial_name if primary_biz else None,
        }

    @classmethod
    def handle_webhook_update(
        cls,
        update: Dict[str, Any],
        db: Session,
        sender_func: Optional[Callable[[int, str], Any]] = None,
    ) -> Dict[str, Any]:
        """Procesa una actualización proveniente de un Webhook de Telegram."""
        message = update.get("message") or update.get("edited_message")
        if not message:
            return {"status": "IGNORED", "reason": "NO_MESSAGE"}

        chat = message.get("chat", {})
        chat_id = chat.get("id")
        from_user = message.get("from", {})
        username = from_user.get("username")
        text = message.get("text", "").strip()

        if not chat_id or not text:
            return {"status": "IGNORED", "reason": "NO_CHAT_OR_TEXT"}

        # Detectar comando /start con argumento
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            payload = parts[1].strip() if len(parts) > 1 else ""

            result = cls.process_start_payload(
                chat_id=chat_id,
                payload=payload,
                db=db,
                telegram_username=username,
            )

            reply_text = result["welcome_message"] if result["success"] else result["message"]

            if sender_func:
                try:
                    sender_func(chat_id, reply_text)
                except Exception as e:
                    logger.error(f"Error despachando respuesta Telegram a {chat_id}: {e}")

            return {
                "status": "PROCESSED",
                "chat_id": chat_id,
                "command": "/start",
                "result": result,
                "reply_sent": bool(sender_func),
            }

        return {"status": "IGNORED", "reason": "UNHANDLED_COMMAND"}
