"""Avisos por Telegram a la administradora comercial (rol ADMIN) sobre el pipeline de extracción DIAN.

Complementa a tech_ops_bot.py: TECH_OPS recibe el diagnóstico técnico y ADMIN recibe un aviso en
lenguaje llano cuando una descarga lenta se reprograma o requiere revisión manual (ADR-008, ADR-015).
Un único callback compuesto decide a quién avisar según la clase del error, para que la API y el
worker local no dupliquen ni omitan avisos.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from dian_automation.db.models import User, DIANExtractionJob, Business
from dian_automation.queue.exceptions import STALE_PROCESSING_CODE, is_slow_error
from dian_automation.telegram.tech_ops_bot import TechOpsAlertBot, create_tech_ops_on_failure_callback

logger = logging.getLogger("admin_alerts")

BOGOTA_TZ = ZoneInfo("America/Bogota")

SLOW_CAUSE_TEXT = "el archivo quedó cargando en la DIAN y no se alcanzó a descargar"
STALE_CAUSE_TEXT = "la descarga no respondió a tiempo (el equipo que descarga puede estar apagado o sin internet)"


def to_bogota_text(value_utc: datetime) -> str:
    """Convierte una fecha naive en UTC (como las guarda la base) a 'dd/mm/AAAA HH:MM' en hora de Bogotá."""
    return value_utc.replace(tzinfo=timezone.utc).astimezone(BOGOTA_TZ).strftime("%d/%m/%Y %H:%M")


def _cause_text(error_code: str) -> str:
    return STALE_CAUSE_TEXT if error_code == STALE_PROCESSING_CODE else SLOW_CAUSE_TEXT


def format_slow_retry_message(
    business_name: str,
    nit: str,
    period: str,
    error_code: str,
    attempt_count: int,
    max_attempts: int,
    next_run_at: datetime,
) -> str:
    """Aviso de una descarga lenta que se reintenta sola. Texto plano: sin trazas ni Markdown."""
    return (
        "⏳ Descarga de la DIAN pendiente\n\n"
        f"Cliente: {business_name}\n"
        f"NIT: {nit}\n"
        f"Periodo: {period}\n"
        f"Motivo: {_cause_text(error_code)}.\n"
        f"Intento {attempt_count} de {max_attempts}.\n\n"
        f"Se reintenta automáticamente a partir del {to_bogota_text(next_run_at)} (hora de Bogotá)."
    )


def format_exhausted_message(
    business_name: str,
    nit: str,
    period: str,
    error_code: str,
    max_attempts: int,
) -> str:
    """Aviso de una descarga lenta que agotó sus intentos."""
    return (
        "🛑 Descarga de la DIAN fallida: requiere revisión manual\n\n"
        f"Cliente: {business_name}\n"
        f"NIT: {nit}\n"
        f"Periodo: {period}\n"
        f"Motivo: {_cause_text(error_code)}.\n"
        f"Se agotaron los {max_attempts} intentos y no se reintentará sola."
    )


def get_admin_chat_ids(db: Session) -> List[int]:
    """Chat ids de los usuarios ADMIN activos con Telegram vinculado."""
    users = (
        db.query(User)
        .filter(
            User.role == "ADMIN",
            User.is_active.is_(True),
            User.telegram_chat_id.isnot(None),
        )
        .all()
    )
    return [u.telegram_chat_id for u in users if u.telegram_chat_id]


def _send_text(chat_ids: List[int], text: str, sender: TechOpsAlertBot, audience: str, who: str) -> Dict[str, Any]:
    """Envía `text` en texto plano a cada chat. Nunca lanza: un fallo de Telegram no debe deshacer nada."""
    if not chat_ids:
        logger.warning(f"No hay destinatarios {audience} con telegram_chat_id configurado.")
        return {"sent": False, "recipients_count": 0, "delivered_count": 0, "reason": "NO_RECIPIENTS"}

    delivered = 0
    for chat_id in chat_ids:
        try:
            result = sender.http_dispatcher("sendMessage", {"chat_id": chat_id, "text": text}, None)
        except Exception as e:
            logger.error(f"Error avisando a {who} (chat {chat_id}): {e}")
            continue
        if result.get("ok"):
            delivered += 1
        else:
            logger.warning(f"Telegram no entregó el aviso a {who} (chat {chat_id}): {result}")
    return {"sent": delivered > 0, "recipients_count": len(chat_ids), "delivered_count": delivered}


def notify_admins(db: Session, text: str, bot: Optional[TechOpsAlertBot] = None) -> Dict[str, Any]:
    """Envía `text` a cada ADMIN activo con chat."""
    return _send_text(get_admin_chat_ids(db), text, bot or TechOpsAlertBot(), "ADMIN", "la administradora")


def notify_tech_ops(db: Session, text: str, bot: Optional[TechOpsAlertBot] = None) -> Dict[str, Any]:
    """Envía `text` (sin botones ni Markdown) a cada TECH_OPS activo con chat."""
    sender = bot or TechOpsAlertBot()
    return _send_text(sender.get_tech_ops_chat_ids(db), text, sender, "TECH_OPS", "Soporte TI")


def notify_slow_failure(
    job: DIANExtractionJob,
    error_code: str,
    db: Session,
    bot: Optional[TechOpsAlertBot] = None,
) -> Dict[str, Any]:
    """Avisa a la administradora de un fallo lento: reintento programado, o revisión manual si se agotó."""
    business = db.query(Business).filter(Business.id == job.business_id).first()
    name = (business.commercial_name or business.legal_name) if business else "Desconocido"
    nit = f"{business.nit}-{business.dv}" if business else "N/A"

    if job.status == "FAILED":
        text = format_exhausted_message(name, nit, job.target_period, error_code, job.max_attempts)
    else:
        text = format_slow_retry_message(
            name, nit, job.target_period, error_code, job.attempt_count, job.max_attempts, job.next_run_at
        )
    return notify_admins(db, text, bot=bot)


def create_failure_alert_callback(
    bot: Optional[TechOpsAlertBot] = None,
    db_session_factory: Optional[Callable[[], Session]] = None,
) -> Callable[[DIANExtractionJob, str, str, Optional[str]], None]:
    """Callback compatible con ExtractionWorker que decide a quién avisar por clase de error.

    - Fallo duro: alerta técnica a TECH_OPS (Story 1.4), sin cambios.
    - Fallo lento con reintentos: solo aviso a ADMIN.
    - Fallo lento agotado (FAILED): aviso a ADMIN y alerta técnica a TECH_OPS.
    """
    alert_bot = bot or TechOpsAlertBot()
    tech_ops_callback = create_tech_ops_on_failure_callback(bot=alert_bot, db_session_factory=db_session_factory)

    def callback(job: DIANExtractionJob, error_code: str, error_detail: str, screenshot_path: Optional[str]):
        if is_slow_error(error_code):
            if db_session_factory:
                db = db_session_factory()
                try:
                    notify_slow_failure(job, error_code, db, bot=alert_bot)
                except Exception as e:
                    logger.error(f"Error avisando a la administradora del trabajo {job.id}: {e}")
                finally:
                    db.close()
            if job.status != "FAILED":
                return
        tech_ops_callback(job, error_code, error_detail, screenshot_path)

    return callback
