"""Módulo de notificaciones salientes de Telegram vía HTTP (httpx)."""

import logging
import os
from typing import Optional
import httpx

logger = logging.getLogger("telegram_notify")
# httpx loguea a nivel INFO la URL completa de cada petición y la de Telegram lleva el token del bot.
logging.getLogger("httpx").setLevel(logging.WARNING)


def send_telegram_message(
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = "Markdown",
) -> bool:
    """Envía un mensaje de Telegram utilizando httpx.

    Nunca loguea la URL ni el token del bot. Si parse_mode está definido y Telegram
    rechaza el mensaje (status != 200 o not ok), reintenta el envío en texto plano sin parse_mode.
    """
    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("TELEGRAM_ADMIN_BOT_TOKEN")
        or os.getenv("TELEGRAM_CLIENT_BOT_TOKEN")
    )
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN no configurado: no se puede enviar mensaje por Telegram.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        response = httpx.post(url, json=payload, timeout=10.0)
        if response.status_code == 200 and bool(response.json().get("ok")):
            return True

        if parse_mode:
            logger.warning(
                "Telegram rechazó el envío con parse_mode=%s (status %d). Reintentando en texto plano.",
                parse_mode,
                response.status_code,
            )
            plain_payload = {"chat_id": chat_id, "text": text}
            retry_resp = httpx.post(url, json=plain_payload, timeout=10.0)
            return retry_resp.status_code == 200 and bool(retry_resp.json().get("ok"))

        return False
    except Exception as exc:
        logger.error("Fallo al enviar mensaje por Telegram (%s).", type(exc).__name__)
        return False
