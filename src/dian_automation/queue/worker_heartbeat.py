"""Latido del worker remoto y vigilancia de su silencio (Story 1.8, ADR-015 puntos 6 y 7).

El worker no tiene canal propio: cada consulta a `/internal/jobs/next` es su latido y la API guarda
la hora del SERVIDOR (nunca la del equipo del worker, cuyo reloj puede estar desalineado).

`WorkerSilenceMonitor` corre en el proceso `scheduler`, esté o no habilitada la programación
semanal: si el worker calla mientras hay trabajo listo, avisa por Telegram a TECH_OPS y ADMIN.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from sqlalchemy.orm import Session

from dian_automation.config import config
from dian_automation.db.models import DIANExtractionJob, WorkerHeartbeat
from dian_automation.telegram.admin_alerts import notify_admins, notify_tech_ops, to_bogota_text
from dian_automation.telegram.tech_ops_bot import TechOpsAlertBot

logger = logging.getLogger("worker_heartbeat")

DEFAULT_WORKER_NAME = "remote"
MAX_WORKER_NAME_LENGTH = 100
ALERT_INTERVAL = timedelta(hours=1)  # no se repite el aviso de silencio antes de esto


def normalize_worker_name(raw: Optional[str]) -> str:
    """Nombre del worker desde la cabecera X-Worker-Name: recortado, y 'remote' si viene vacío."""
    name = (raw or "").strip()[:MAX_WORKER_NAME_LENGTH].strip()
    return name or DEFAULT_WORKER_NAME


def _naive_utc(now: Optional[datetime]) -> datetime:
    """`now` como datetime naive en UTC (como guarda las fechas la base); ausente = ahora."""
    if now is None:
        return datetime.utcnow()
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None)
    return now


def record_heartbeat(db: Session, name: str, now: Optional[datetime] = None) -> WorkerHeartbeat:
    """Guarda `last_seen_at = ahora` para `name`, creando la fila si no existe.

    Un latido nuevo reinicia `last_alert_at`: si el worker vuelve y luego vuelve a callar, el aviso
    sale en cuanto corresponda y no espera la hora del silencio anterior.
    """
    moment = _naive_utc(now)
    row = db.query(WorkerHeartbeat).filter(WorkerHeartbeat.name == name).first()
    if row is None:
        row = WorkerHeartbeat(name=name, last_seen_at=moment)
        db.add(row)
    else:
        row.last_seen_at = moment
        row.last_alert_at = None
    db.commit()
    return row


def _format_duration(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def format_silence_message(
    last_seen_at: Optional[datetime], reference_at: datetime, silence: timedelta, pending_jobs: int
) -> str:
    """Aviso de que el worker no responde. Texto plano, hora de Bogotá."""
    if last_seen_at is None:
        since = f"Nunca ha reportado (hay trabajos listos desde {to_bogota_text(reference_at)}, hora de Bogotá)"
    else:
        since = f"Último contacto: {to_bogota_text(last_seen_at)} (hora de Bogotá), hace {_format_duration(silence)}"
    return (
        "⚠️ El worker de descargas no responde\n\n"
        f"{since}.\n"
        f"Trabajos pendientes: {pending_jobs}.\n\n"
        "Las descargas de la DIAN no avanzan hasta que vuelva. Revisa que el equipo del worker esté "
        "encendido, con internet y con el worker corriendo (ver docs/DEPLOYMENT.md)."
    )


class WorkerSilenceMonitor:
    """Decide y envía el aviso de silencio del worker; `check(now)` es un ciclo, con reloj inyectable."""

    def __init__(
        self,
        db_session_factory: Callable[[], Session],
        silence_minutes: Optional[int] = None,
        stale_seconds: Optional[int] = None,
        bot: Optional[TechOpsAlertBot] = None,
    ):
        minutes = config.worker_silence_minutes if silence_minutes is None else silence_minutes
        if minutes <= 0:
            raise ValueError(f"WORKER_SILENCE_MINUTES debe ser mayor que 0; recibido {minutes}")
        self.db_session_factory = db_session_factory
        self.silence = timedelta(minutes=minutes)
        self.stale = timedelta(seconds=config.stale_processing_seconds if stale_seconds is None else stale_seconds)
        self.bot = bot

    def check(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        moment = _naive_utc(now)
        db = self.db_session_factory()
        try:
            return self._check(db, moment)
        finally:
            db.close()

    def _check(self, db: Session, now: datetime) -> Dict[str, Any]:
        runnable = (
            db.query(DIANExtractionJob.next_run_at)
            .filter(DIANExtractionJob.status == "ENQUEUED", DIANExtractionJob.next_run_at <= now)
            .all()
        )
        processing = db.query(DIANExtractionJob.started_at).filter(DIANExtractionJob.status == "PROCESSING").all()
        cutoff = now - self.stale
        # Un PROCESSING vigente es un worker ocupado: una descarga legítima no consulta la API mientras corre
        if any(started is not None and started >= cutoff for (started,) in processing):
            return {"alerted": False, "reason": "WORKER_BUSY"}
        stale_started = [started for (started,) in processing if started is not None]
        pending = len(runnable) + len(processing)
        if pending == 0:
            return {"alerted": False, "reason": "NO_PENDING_JOBS"}

        rows = db.query(WorkerHeartbeat).all()
        seen = [r for r in rows if r.last_seen_at is not None]
        last_seen = max((r.last_seen_at for r in seen), default=None)
        # Sin ningún latido, el silencio se mide desde el trabajo ejecutable más antiguo
        reference_candidates = [next_run for (next_run,) in runnable] + stale_started
        if last_seen is None and not reference_candidates:
            return {"alerted": False, "reason": "NO_REFERENCE"}
        reference = last_seen or min(reference_candidates)
        silence = now - reference
        if silence <= self.silence:
            return {"alerted": False, "reason": "WORKER_ALIVE"}

        last_alert = max((r.last_alert_at for r in rows if r.last_alert_at is not None), default=None)
        if last_alert is not None and now - last_alert < ALERT_INTERVAL:
            return {"alerted": False, "reason": "ALREADY_ALERTED"}

        text = format_silence_message(last_seen, reference, silence, pending)
        tech = notify_tech_ops(db, text, bot=self.bot)
        admin = notify_admins(db, text, bot=self.bot)
        delivered = bool(tech.get("sent") or admin.get("sent"))
        if delivered:
            holder = max(seen, key=lambda r: r.last_seen_at, default=None) or (rows[0] if rows else None)
            if holder is None:
                holder = WorkerHeartbeat(name=DEFAULT_WORKER_NAME, last_seen_at=None)
                db.add(holder)
            holder.last_alert_at = now
            db.commit()
            logger.warning(f"Aviso de silencio del worker enviado ({pending} trabajos pendientes).")
        else:
            logger.warning("El worker no responde pero el aviso no llegó a nadie; se reintenta en el siguiente ciclo.")
        return {"alerted": delivered, "reason": "WORKER_SILENT", "pending_jobs": pending}
