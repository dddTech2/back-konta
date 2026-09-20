"""Programador semanal de descargas DIAN (Story 1.7, ADR-015 punto 5).

Corre en el servidor (el que tiene acceso a la base de datos) y solo ENCOLA: nunca abre la DIAN ni
llama a `run_flow`. El espaciado entre clientes lo hace la cola (concurrencia 1 y 15 minutos tras
cada éxito), así que aquí todos los trabajos se encolan juntos y en orden de antigüedad.

Cada ciclo revisa la hora en America/Bogota. Dentro de la ventana [hora, hora + 1 h):
- el día de la semana configurado (por defecto domingo) encola el mes en curso de cada negocio;
- los días 1 a 5 del mes encola el cierre del mes anterior de los negocios que aún no lo tienen.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from dian_automation.db.models import Business, DIANExtractionJob
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.subscriptions.lockout_service import SubscriptionLockoutService

logger = logging.getLogger("scheduler")

BOGOTA_TZ = ZoneInfo("America/Bogota")
CLOSING_LAST_DAY = 5  # el cierre del mes anterior se intenta los días 1 a 5
ACTIVE_JOB_STATUSES = ("ENQUEUED", "PROCESSING")
WEEKDAY_NAMES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def _to_bogota(now: Optional[datetime]) -> datetime:
    """Hora de Bogotá de `now`; un valor naive se toma como UTC (como guarda las fechas la base)."""
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(BOGOTA_TZ)


def _bogota_to_utc_naive(year: int, month: int, day: int, hour: int = 0) -> datetime:
    """Instante local de Bogotá como datetime naive en UTC, para comparar con las columnas de la base."""
    local = datetime(year, month, day, hour, tzinfo=BOGOTA_TZ)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _period(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _previous_month(year: int, month: int) -> tuple:
    return (year - 1, 12) if month == 1 else (year, month - 1)


class ExtractionScheduler:
    """Decide y encola las extracciones semanales y de cierre de mes."""

    def __init__(self, db_session_factory: Callable[[], Session], weekday: int = 6, hour: int = 3):
        if not 0 <= weekday <= 6:
            raise ValueError(f"SCHEDULER_WEEKDAY debe estar entre 0 (lunes) y 6 (domingo); recibido {weekday}")
        if not 0 <= hour <= 23:
            raise ValueError(f"SCHEDULER_HOUR debe estar entre 0 y 23; recibido {hour}")
        self.db_session_factory = db_session_factory
        self.weekday = weekday
        self.hour = hour

    def describe(self) -> str:
        return f"{WEEKDAY_NAMES[self.weekday]} a las {self.hour:02d}:00 (America/Bogota)"

    def tick(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Un ciclo del programador. `now` inyecta el reloj (naive = UTC) para las pruebas."""
        local = _to_bogota(now)
        if local.hour != self.hour:
            return {"ran": False, "reason": "OUTSIDE_WINDOW", "enqueued": []}

        closing_period = None
        if local.day <= CLOSING_LAST_DAY:
            closing_period = _period(*_previous_month(local.year, local.month))
        periods: List[str] = [closing_period] if closing_period else []
        if local.weekday() == self.weekday:
            periods.append(_period(local.year, local.month))
        if not periods:
            return {"ran": False, "reason": "NOT_A_RUN_DAY", "enqueued": []}

        window_start = _bogota_to_utc_naive(local.year, local.month, local.day, self.hour)
        month_start = _bogota_to_utc_naive(local.year, local.month, 1)

        enqueued: List[Dict[str, str]] = []
        skipped = 0
        errors = 0
        db = self.db_session_factory()
        try:
            businesses = self._eligible_businesses(db)
            for period in periods:
                for business in businesses:
                    if period == closing_period and self._has_closing_success(db, business.id, period, month_start):
                        skipped += 1
                        continue
                    if self._already_scheduled(db, business.id, period, window_start):
                        skipped += 1
                        continue
                    try:
                        job = ExtractionQueueManager.enqueue_job(business.id, period, db)
                    except Exception:
                        db.rollback()
                        errors += 1
                        logger.exception(f"No se pudo encolar {period} del negocio {business.id}")
                        continue
                    enqueued.append({"job_id": job.id, "business_id": business.id, "period": period})
        finally:
            db.close()

        return {"ran": True, "periods": periods, "enqueued": enqueued, "skipped": skipped, "errors": errors}

    def run_loop(
        self,
        enabled: bool,
        interval: int = 60,
        sleep: Callable[[float], None] = time.sleep,
        now_func: Optional[Callable[[], datetime]] = None,
        max_iterations: Optional[int] = None,
    ) -> None:
        """Bucle del proceso. Deshabilitado sigue vivo (para que Docker no lo reinicie) pero no encola."""
        if enabled:
            logger.info(f"Programador habilitado: corrida semanal el {self.describe()}; cierre de mes los días 1 a 5.")
        else:
            logger.info("Programador deshabilitado (SCHEDULER_ENABLED distinto de 'true'): no se encolará nada.")

        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            if enabled:
                try:
                    result = self.tick(now_func() if now_func else None)
                    if result["enqueued"] or result.get("errors"):
                        logger.info(
                            f"Ciclo del programador: {len(result['enqueued'])} trabajos encolados "
                            f"({', '.join(result['periods'])}), {result['skipped']} omitidos, {result['errors']} con error."
                        )
                except Exception:
                    logger.exception("Error en el ciclo del programador; se reintenta en el siguiente.")
            sleep(interval)

    @staticmethod
    def _eligible_businesses(db: Session) -> List[Business]:
        """Negocios activos cuyo cliente no está bloqueado, de la extracción exitosa más antigua a la más reciente."""
        last_success = dict(
            db.query(DIANExtractionJob.business_id, func.max(DIANExtractionJob.finished_at))
            .filter(DIANExtractionJob.status == "SUCCESS")
            .group_by(DIANExtractionJob.business_id)
            .all()
        )
        businesses = db.query(Business).filter(Business.is_active.is_(True)).all()
        eligible = [b for b in businesses if not SubscriptionLockoutService.is_client_blocked(db, b.client_id)]
        # Sin extracción exitosa primero; luego por antigüedad de la última, y por alta del negocio
        eligible.sort(
            key=lambda b: (
                last_success.get(b.id) is not None,
                last_success.get(b.id) or datetime.min,
                b.created_at,
                b.id,
            )
        )
        return eligible

    @staticmethod
    def _has_closing_success(db: Session, business_id: str, period: str, month_start: datetime) -> bool:
        """El mes anterior ya se descargó después de cerrar (con `finished_at` desde el día 1 del mes en curso)."""
        return (
            db.query(DIANExtractionJob.id)
            .filter(
                DIANExtractionJob.business_id == business_id,
                DIANExtractionJob.target_period == period,
                DIANExtractionJob.status == "SUCCESS",
                DIANExtractionJob.finished_at >= month_start,
            )
            .first()
            is not None
        )

    @staticmethod
    def _already_scheduled(db: Session, business_id: str, period: str, window_start: datetime) -> bool:
        """Hay un trabajo del mismo negocio y periodo pendiente, o creado desde que abrió la ventana de hoy.

        Lo segundo evita que el ciclo de 60 s vuelva a encolar apenas el primer trabajo termina en SUCCESS.
        """
        return (
            db.query(DIANExtractionJob.id)
            .filter(
                DIANExtractionJob.business_id == business_id,
                DIANExtractionJob.target_period == period,
                (DIANExtractionJob.status.in_(ACTIVE_JOB_STATUSES)) | (DIANExtractionJob.created_at >= window_start),
            )
            .first()
            is not None
        )
