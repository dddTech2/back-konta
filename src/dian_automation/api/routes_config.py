"""Configuración pública mínima que el frontend necesita para enlazarse con el bot real."""

import os
import logging
from typing import Optional
import httpx
from fastapi import APIRouter

# httpx loguea a nivel INFO la URL completa de cada petición, y la API de Telegram exige el
# token embebido en la URL (.../bot<token>/getMe) -- sin esto, el token quedaría expuesto en
# texto plano en los logs del servidor.
logging.getLogger("httpx").setLevel(logging.WARNING)

router = APIRouter(prefix="/api", tags=["Config"])

_cached_bot_username: Optional[str] = None


def _resolve_bot_username() -> Optional[str]:
    """Obtiene el @username real del bot configurado en .env consultando la API de Telegram.

    Usa el mismo TELEGRAM_BOT_TOKEN que corre en el bot de Telegram (cli/telegram_bot.py), así el frontend
    nunca apunta a un nombre de bot inventado o desactualizado (ej. 'KontaBot' genérico,
    que en Telegram puede pertenecer a un bot de un tercero).
    """
    global _cached_bot_username
    if _cached_bot_username:
        return _cached_bot_username

    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("TELEGRAM_ADMIN_BOT_TOKEN")
        or os.getenv("TELEGRAM_CLIENT_BOT_TOKEN")
    )
    if not token:
        return None

    try:
        res = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5.0)
        if res.status_code == 200:
            _cached_bot_username = res.json().get("result", {}).get("username")
    except Exception:
        pass

    return _cached_bot_username


@router.get("/config")
def get_public_config():
    """Config pública para el frontend (nunca expone el token, solo el @username resultante)."""
    return {"telegram_bot_username": _resolve_bot_username()}
