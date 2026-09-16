"""Bot de Telegram para Soporte Técnico (Tech-Ops).

Despacha alertas enriquecidas ante fallos en extracciones DIAN a los usuarios
con rol TECH_OPS, incluyendo diagnóstico, tiempo, captura fotográfica del
portal y botón interactivo para reintento inmediato.
"""

import os
import json
import logging
import uuid
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable
from sqlalchemy.orm import Session

from dian_automation.db.models import User, DIANExtractionJob, Business

logger = logging.getLogger("tech_ops_bot")


class TechOpsAlertBot:
    """Gestor de notificaciones y callbacks para el equipo de Tech-Ops."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        http_dispatcher: Optional[Callable[[str, Dict[str, Any], Optional[Dict[str, Any]]], Dict[str, Any]]] = None,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_TECH_OPS_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
        self.http_dispatcher = http_dispatcher or self._default_http_dispatcher

    def _default_http_dispatcher(
        self,
        endpoint: str,
        data: Dict[str, Any],
        files: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Despachador HTTP por defecto hacia la API oficial de Telegram."""
        if not self.bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN no configurado. Despacho omitido.")
            return {"ok": False, "description": "Token de bot no configurado"}

        url = f"https://api.telegram.org/bot{self.bot_token}/{endpoint}"

        try:
            if files and "photo" in files and os.path.exists(files["photo"]):
                # Multipart/form-data para envío de fotos
                boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
                body_parts = []

                for key, val in data.items():
                    if val is not None:
                        body_parts.append(f"--{boundary}\r\n".encode("utf-8"))
                        body_parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
                        body_parts.append(f"{val}\r\n".encode("utf-8"))

                # Adjuntar foto
                photo_path = files["photo"]
                filename = os.path.basename(photo_path)
                with open(photo_path, "rb") as f:
                    file_bytes = f.read()

                body_parts.append(f"--{boundary}\r\n".encode("utf-8"))
                body_parts.append(
                    f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode("utf-8")
                )
                body_parts.append(b"Content-Type: image/png\r\n\r\n")
                body_parts.append(file_bytes)
                body_parts.append(b"\r\n")
                body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))

                payload = b"".join(body_parts)
                headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
            else:
                # JSON estándar para mensajes de texto
                payload = json.dumps(data).encode("utf-8")
                headers = {"Content-Type": "application/json"}

            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as response:
                res_body = response.read().decode("utf-8")
                return json.loads(res_body)

        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8") if e.fp else str(e)
            logger.error(f"Error HTTP al enviar a Telegram ({endpoint}): {e.code} - {err_msg}")
            return {"ok": False, "error_code": e.code, "description": err_msg}
        except Exception as e:
            logger.error(f"Fallo de conexión enviando a Telegram ({endpoint}): {e}")
            return {"ok": False, "description": str(e)}

    @staticmethod
    def format_alert_message(
        business_name: str,
        nit: str,
        period: str,
        error_code: str,
        error_detail: str,
        attempt_count: int,
        max_attempts: int,
        elapsed_seconds: Optional[float] = None,
    ) -> str:
        """Formatea el mensaje de alerta técnica en Markdown para Telegram."""
        elapsed_str = f"{elapsed_seconds:.1f}s" if elapsed_seconds is not None else "N/A"
        return (
            "🚨 *ALERTA TÉCNICA - EXTRACCIÓN DIAN* 🚨\n\n"
            f"🏢 *Empresa:* {business_name}\n"
            f"🆔 *NIT:* `{nit}`\n"
            f"📅 *Periodo:* `{period}`\n"
            f"⚠️ *Código de Error:* `{error_code}`\n"
            f"📝 *Detalle:* {error_detail}\n"
            f"🔄 *Intento:* {attempt_count}/{max_attempts}\n"
            f"⏱️ *Tiempo Transcurrido:* {elapsed_str}\n\n"
            "⚠️ _La cola ha aplicado backoff de 1 hora. Puedes forzar la reanudación inmediata abajo._"
        )

    @staticmethod
    def get_retry_inline_keyboard(job_id: str) -> Dict[str, Any]:
        """Genera el teclado interactivo con el botón de reintento."""
        return {
            "inline_keyboard": [
                [
                    {"text": "🔄 Reintentar Ahora", "callback_data": f"retry:{job_id}"}
                ]
            ]
        }

    def get_tech_ops_chat_ids(self, db: Session) -> List[int]:
        """Obtiene la lista de chat_ids registrados para usuarios con rol TECH_OPS."""
        users = (
            db.query(User)
            .filter(
                User.role == "TECH_OPS",
                User.is_active.is_(True),
                User.telegram_chat_id.isnot(None),
            )
            .all()
        )
        return [u.telegram_chat_id for u in users if u.telegram_chat_id]

    def send_failure_alert(
        self,
        job: DIANExtractionJob,
        db: Session,
        chat_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Despacha la alerta de fallo con imagen (si existe) y botón interactivo."""
        target_chat_ids = chat_ids or self.get_tech_ops_chat_ids(db)
        if not target_chat_ids:
            logger.warning("No hay destinatarios TECH_OPS con telegram_chat_id configurado.")
            return {"sent": False, "recipients_count": 0, "reason": "NO_RECIPIENTS"}

        # Obtener datos de la empresa
        biz = db.query(Business).filter(Business.id == job.business_id).first()
        biz_name = biz.commercial_name if biz and biz.commercial_name else (biz.legal_name if biz else "Desconocida")
        nit_str = f"{biz.nit}-{biz.dv}" if biz else "N/A"

        # Calcular tiempo transcurrido
        elapsed = None
        if job.started_at and job.finished_at:
            elapsed = (job.finished_at - job.started_at).total_seconds()

        text = self.format_alert_message(
            business_name=biz_name,
            nit=nit_str,
            period=job.target_period,
            error_code=job.error_code or "EXTRACTION_ERROR",
            error_detail=job.error_detail or "Error no especificado",
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            elapsed_seconds=elapsed,
        )
        reply_markup = json.dumps(self.get_retry_inline_keyboard(job.id))

        has_screenshot = job.screenshot_path and os.path.exists(job.screenshot_path)
        endpoint = "sendPhoto" if has_screenshot else "sendMessage"

        dispatch_results = []
        for chat_id in target_chat_ids:
            if has_screenshot:
                data = {
                    "chat_id": chat_id,
                    "caption": text,
                    "parse_mode": "Markdown",
                    "reply_markup": reply_markup,
                }
                files = {"photo": job.screenshot_path}
            else:
                data = {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "reply_markup": reply_markup,
                }
                files = None

            res = self.http_dispatcher(endpoint, data, files)
            dispatch_results.append({"chat_id": chat_id, "result": res})

        return {
            "sent": True,
            "recipients_count": len(target_chat_ids),
            "endpoint": endpoint,
            "has_screenshot": bool(has_screenshot),
            "dispatches": dispatch_results,
        }

    @classmethod
    def handle_retry_callback(cls, callback_data: str, db: Session) -> Dict[str, Any]:
        """Procesa el clic en el botón '🔄 Reintentar Ahora' de Telegram.
        
        Adelanta la próxima ejecución a la hora actual y restaura el estado a ENQUEUED.
        """
        if not callback_data.startswith("retry:"):
            return {"success": False, "message": "Acción de callback no reconocida"}

        job_id = callback_data.split("retry:", 1)[1].strip()
        job = db.query(DIANExtractionJob).filter(DIANExtractionJob.id == job_id).first()
        if not job:
            return {"success": False, "job_id": job_id, "message": "Trabajo de extracción no encontrado"}

        # Reprogramar ejecución inmediata
        job.next_run_at = datetime.utcnow()
        if job.status in ("FAILED", "PROCESSING"):
            job.status = "ENQUEUED"

        db.commit()
        db.refresh(job)

        return {
            "success": True,
            "job_id": job.id,
            "new_status": job.status,
            "next_run_at": job.next_run_at.isoformat(),
            "message": "✅ Extracción reprogramada para ejecución inmediata.",
        }


def create_tech_ops_on_failure_callback(
    bot: Optional[TechOpsAlertBot] = None,
    db_session_factory: Optional[Callable[[], Session]] = None,
) -> Callable[[DIANExtractionJob, str, str, Optional[str]], None]:
    """Crea un callback compatible con ExtractionWorker para disparar alertas a Tech-Ops."""
    alert_bot = bot or TechOpsAlertBot()

    def callback(job: DIANExtractionJob, error_code: str, error_detail: str, screenshot_path: Optional[str]):
        if db_session_factory:
            db = db_session_factory()
            try:
                alert_bot.send_failure_alert(job=job, db=db)
            finally:
                db.close()
        else:
            logger.info(f"[TechOpsAlert] Alerta generada para job {job.id} con error {error_code}")

    return callback
