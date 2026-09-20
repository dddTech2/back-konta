"""Gestor de cola de extracciones DIAN con persistencia en base de datos."""

from datetime import datetime, timedelta
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import asc, or_

from dian_automation.config import config
from dian_automation.db.models import DIANExtractionJob, Business
from dian_automation.queue.exceptions import STALE_PROCESSING_CODE, is_slow_error
from dian_automation.queue.redis_signal import notify_job_ready


class ExtractionQueueManager:
    """Gestiona el ciclo de vida de los trabajos de extracción encolados."""

    SUCCESS_PACING_SECONDS = 900  # 15 minutos de espaciado obligatorio tras éxito
    FAILURE_BACKOFF_SECONDS = 3600  # 1 hora de pausa ante fallos
    SLOW_RETRY_SECONDS = 21600  # 6 horas de reintento para descargas lentas (solo ese trabajo)

    @classmethod
    def enqueue_job(
        cls,
        business_id: str,
        target_period: str,
        db: Session,
        delay_seconds: int = 0,
    ) -> DIANExtractionJob:
        """Encola un nuevo trabajo de extracción para una empresa y periodo."""
        # Verificar existencia de la empresa
        biz = db.query(Business).filter(Business.id == business_id).first()
        if not biz:
            raise ValueError(f"No existe la empresa con ID '{business_id}'")

        now = datetime.utcnow()
        next_run = now + timedelta(seconds=delay_seconds)

        job = DIANExtractionJob(
            business_id=business_id,
            target_period=target_period,
            status="ENQUEUED",
            attempt_count=0,
            next_run_at=next_run,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        notify_job_ready(job.id)
        return job

    @classmethod
    def get_next_runnable_job(cls, db: Session) -> Optional[DIANExtractionJob]:
        """Obtiene el trabajo más antiguo listo para ejecutarse (concurrencia 1)."""
        now = datetime.utcnow()
        # Verificar que no haya ningún trabajo en proceso actualmente (concurrencia estricta 1)
        running = db.query(DIANExtractionJob).filter(DIANExtractionJob.status == "PROCESSING").first()
        if running:
            return None

        job = (
            db.query(DIANExtractionJob)
            .filter(
                DIANExtractionJob.status == "ENQUEUED",
                DIANExtractionJob.next_run_at <= now,
            )
            .order_by(asc(DIANExtractionJob.next_run_at), asc(DIANExtractionJob.created_at))
            .first()
        )
        return job

    @classmethod
    def mark_job_processing(cls, job_id: str, db: Session) -> DIANExtractionJob:
        """Marca un trabajo como en ejecución."""
        job = db.query(DIANExtractionJob).filter(DIANExtractionJob.id == job_id).first()
        if not job:
            raise ValueError(f"Trabajo con ID '{job_id}' no encontrado")

        job.status = "PROCESSING"
        job.started_at = datetime.utcnow()
        db.commit()
        db.refresh(job)
        return job

    @classmethod
    def claim_job(cls, job_id: str, db: Session) -> Optional[DIANExtractionJob]:
        """Reclama atómicamente un trabajo encolado para su procesamiento exclusivo.

        Aplica un UPDATE condicional (status == ENQUEUED) para evitar condiciones de carrera entre
        múltiples workers concurrentes. Si el trabajo ya fue tomado o no está encolado, devuelve None.
        """
        rows_updated = (
            db.query(DIANExtractionJob)
            .filter(DIANExtractionJob.id == job_id, DIANExtractionJob.status == "ENQUEUED")
            .update({"status": "PROCESSING", "started_at": datetime.utcnow()}, synchronize_session=False)
        )
        db.commit()
        if rows_updated == 0:
            return None

        job = db.query(DIANExtractionJob).filter(DIANExtractionJob.id == job_id).first()
        if job is not None:
            db.refresh(job)
        return job

    @classmethod
    def mark_job_success(
        cls,
        job_id: str,
        zip_path: str,
        db: Session,
        pacing_seconds: Optional[int] = None,
    ) -> DIANExtractionJob:
        """Marca un trabajo como exitoso y programa el espaciado obligatorio de 15 minutos."""
        job = db.query(DIANExtractionJob).filter(DIANExtractionJob.id == job_id).first()
        if not job:
            raise ValueError(f"Trabajo con ID '{job_id}' no encontrado")

        now = datetime.utcnow()
        job.status = "SUCCESS"
        job.finished_at = now
        job.zip_path = zip_path
        job.error_code = None
        job.error_detail = None

        # Política de espaciado obligatorio: reprogramar cualquier job encolado para que no inicie antes de now + pacing_seconds
        pacing = pacing_seconds if pacing_seconds is not None else cls.SUCCESS_PACING_SECONDS
        earliest_next_run = now + timedelta(seconds=pacing)

        pending_jobs = (
            db.query(DIANExtractionJob)
            .filter(
                DIANExtractionJob.status == "ENQUEUED",
                DIANExtractionJob.next_run_at < earliest_next_run,
            )
            .all()
        )

        for p_job in pending_jobs:
            p_job.next_run_at = earliest_next_run

        db.commit()
        db.refresh(job)
        return job

    @classmethod
    def mark_job_failed(
        cls,
        job_id: str,
        error_code: str,
        error_detail: str,
        db: Session,
        screenshot_path: Optional[str] = None,
        backoff_seconds: Optional[int] = None,
        slow_retry_seconds: Optional[int] = None,
    ) -> DIANExtractionJob:
        """Gestiona el fallo de un trabajo, incrementando reintentos y aplicando backoff.

        Un fallo lento (is_slow_error) reprograma solo ese trabajo a +6 h; un fallo duro aplica el
        backoff de 1 h y pausa también los demás trabajos encolados.
        """
        job = db.query(DIANExtractionJob).filter(DIANExtractionJob.id == job_id).first()
        if not job:
            raise ValueError(f"Trabajo con ID '{job_id}' no encontrado")

        now = datetime.utcnow()
        job.attempt_count += 1
        job.error_code = error_code
        job.error_detail = error_detail
        job.screenshot_path = screenshot_path
        job.finished_at = now

        if is_slow_error(error_code):
            delay = slow_retry_seconds if slow_retry_seconds is not None else cls.SLOW_RETRY_SECONDS
            if job.attempt_count < job.max_attempts:
                job.status = "ENQUEUED"
                job.next_run_at = now + timedelta(seconds=delay)
            else:
                job.status = "FAILED"
            db.commit()
            db.refresh(job)
            return job

        backoff = backoff_seconds if backoff_seconds is not None else cls.FAILURE_BACKOFF_SECONDS
        earliest_next_run = now + timedelta(seconds=backoff)

        if job.attempt_count < job.max_attempts:
            job.status = "ENQUEUED"
            job.next_run_at = earliest_next_run  # Se mueve al final tras la pausa de 1 hora
        else:
            job.status = "FAILED"

        # Pausar también los demás trabajos encolados durante el tiempo de backoff
        pending_jobs = (
            db.query(DIANExtractionJob)
            .filter(
                DIANExtractionJob.status == "ENQUEUED",
                DIANExtractionJob.id != job_id,
                DIANExtractionJob.next_run_at < earliest_next_run,
            )
            .all()
        )
        for p_job in pending_jobs:
            p_job.next_run_at = earliest_next_run

        db.commit()
        db.refresh(job)
        return job

    @classmethod
    def recover_stale_jobs(cls, db: Session, stale_seconds: Optional[int] = None) -> List[DIANExtractionJob]:
        """Libera los trabajos PROCESSING sin respuesta: los trata como fallo lento STALE_PROCESSING.

        Sin esto, un worker apagado o una subida fallida dejan un PROCESSING que bloquea la cola para
        siempre (get_next_runnable_job devuelve None mientras exista uno). Devuelve los recuperados.
        """
        threshold = stale_seconds if stale_seconds is not None else config.stale_processing_seconds
        cutoff = datetime.utcnow() - timedelta(seconds=threshold)
        stale_ids = [
            row[0]
            for row in db.query(DIANExtractionJob.id)
            .filter(
                DIANExtractionJob.status == "PROCESSING",
                or_(DIANExtractionJob.started_at.is_(None), DIANExtractionJob.started_at < cutoff),
            )
            .all()
        ]
        return [
            cls.mark_job_failed(
                job_id=job_id,
                error_code=STALE_PROCESSING_CODE,
                error_detail=f"Sin respuesta del worker tras {threshold} s en PROCESSING; se reprograma.",
                db=db,
            )
            for job_id in stale_ids
        ]
