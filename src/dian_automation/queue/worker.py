"""Worker secuencial de extracciones DIAN con pacing de 15 minutos e ingesta automática."""

import logging
import time
from datetime import datetime
from typing import Optional, Callable, Dict, Any
from sqlalchemy.orm import Session

from dian_automation.db.database import SessionLocal
from dian_automation.db.models import DIANExtractionJob, Business
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.extraction.xlsx_parser import DIANXLSXParser, DIANParseError

logger = logging.getLogger("dian_worker")


class ExtractionWorker:
    """Worker de ejecución secuencial de trabajos de extracción DIAN."""

    def __init__(self, db_session_factory: Callable[[], Session] = SessionLocal):
        self.db_session_factory = db_session_factory

    def execute_job(
        self,
        job_id: str,
        extractor_func: Optional[Callable[[Business, str], str]] = None,
        parser_func: Optional[Callable[[str, str, Session, str], Dict[str, Any]]] = None,
        on_failure_callback: Optional[Callable[[DIANExtractionJob, str, str, Optional[str]], None]] = None,
        pacing_seconds: Optional[int] = None,
        backoff_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Ejecuta un trabajo específico, orquestando extracción, parsing y pacing."""
        db = self.db_session_factory()
        try:
            job = ExtractionQueueManager.mark_job_processing(job_id=job_id, db=db)
            business = db.query(Business).filter(Business.id == job.business_id).first()
            if not business:
                raise ValueError(f"Empresa asociada al trabajo {job_id} no encontrada")

            logger.info(f"Iniciando extracción para {business.commercial_name} ({business.nit}) periodo {job.target_period}")

            # 1. Extracción (Descarga desde portal DIAN)
            if extractor_func:
                zip_path = extractor_func(business, job.target_period)
            else:
                # Flujo real por defecto importando dian_flow
                import asyncio
                from dian_automation.dian_flow import run_flow

                login_type = "empresa" if business.taxpayer_type == "PERSONA_JURIDICA" else "persona"
                zip_path = asyncio.run(
                    run_flow(
                        target_month=job.target_period,
                        login_type=login_type,
                        representative_code=business.legal_rep_doc if login_type == "empresa" else None,
                        company_nit=business.nit if login_type == "empresa" else None,
                        person_code=business.nit if login_type == "persona" else None,
                    )
                )

            # 2. Ingesta y parsing de facturas
            logger.info(f"Extracción exitosa. Procesando archivo {zip_path}")
            if parser_func:
                parse_result = parser_func(zip_path, business.id, db, job.id)
            else:
                parse_result = DIANXLSXParser.parse_zip(
                    zip_path=zip_path,
                    business_id=business.id,
                    db=db,
                    job_id=job.id,
                )

            # 3. Marcar éxito y programar espaciado de 15 minutos
            ExtractionQueueManager.mark_job_success(
                job_id=job.id,
                zip_path=zip_path,
                db=db,
                pacing_seconds=pacing_seconds,
            )
            logger.info(f"Trabajo {job.id} completado con éxito. Pacing de 15 min aplicado.")

            return {
                "status": "SUCCESS",
                "job_id": job.id,
                "zip_path": zip_path,
                "parse_result": parse_result,
            }

        except Exception as e:
            logger.error(f"Fallo en trabajo de extracción {job_id}: {e}", exc_info=True)
            
            # Obtener código tipificado si es DIANExtractionError
            from dian_automation.queue.exceptions import DIANExtractionError
            if isinstance(e, DIANExtractionError):
                error_code = e.code
            else:
                error_code = type(e).__name__
            error_detail = str(e)

            # Buscar y asociar captura de pantalla de evidencia si existe
            import os, shutil
            screenshot_path = None
            evidence_candidates = [
                "screenshot_turnstile_timeout.png",
                "screenshot_turnstile_widget.png",
                "screenshot_espera.png",
                "screenshot_tabla_reportes.png",
                "screenshot_antes_exportar.png",
            ]
            for cand in evidence_candidates:
                if os.path.exists(cand):
                    os.makedirs("downloads", exist_ok=True)
                    dest = f"downloads/evidence_{job_id}.png"
                    try:
                        shutil.copyfile(cand, dest)
                        screenshot_path = dest
                        break
                    except Exception:
                        screenshot_path = cand
                        break

            job = ExtractionQueueManager.mark_job_failed(
                job_id=job_id,
                error_code=error_code,
                error_detail=error_detail,
                screenshot_path=screenshot_path,
                db=db,
                backoff_seconds=backoff_seconds,
            )

            # Invocar callback on_failure (para notificar al bot de TI)
            if on_failure_callback:
                try:
                    on_failure_callback(job, error_code, error_detail, screenshot_path)
                except Exception as cb_err:
                    logger.error(f"Error en callback on_failure: {cb_err}")

            return {
                "status": "FAILED" if job.status == "FAILED" else "RETRY_SCHEDULED",
                "job_id": job.id,
                "error_code": error_code,
                "error_detail": error_detail,
                "screenshot_path": screenshot_path,
                "attempt_count": job.attempt_count,
            }
        finally:
            db.close()

    def run_once(
        self,
        extractor_func: Optional[Callable] = None,
        parser_func: Optional[Callable] = None,
        on_failure_callback: Optional[Callable] = None,
        pacing_seconds: Optional[int] = None,
        backoff_seconds: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Busca y ejecuta el siguiente trabajo listo en la cola."""
        db = self.db_session_factory()
        try:
            job = ExtractionQueueManager.get_next_runnable_job(db=db)
            if not job:
                return None
            job_id = job.id
        finally:
            db.close()

        return self.execute_job(
            job_id=job_id,
            extractor_func=extractor_func,
            parser_func=parser_func,
            on_failure_callback=on_failure_callback,
            pacing_seconds=pacing_seconds,
            backoff_seconds=backoff_seconds,
        )

    def run_loop(
        self,
        poll_interval: int = 5,
        max_jobs: Optional[int] = None,
        extractor_func: Optional[Callable] = None,
        parser_func: Optional[Callable] = None,
        on_failure_callback: Optional[Callable] = None,
    ):
        """Bucle principal de ejecución del worker daemon."""
        jobs_processed = 0
        logger.info("Iniciando bucle de ExtractionWorker (Concurrencia: 1)...")

        while True:
            result = self.run_once(
                extractor_func=extractor_func,
                parser_func=parser_func,
                on_failure_callback=on_failure_callback,
            )
            if result:
                jobs_processed += 1
                if max_jobs and jobs_processed >= max_jobs:
                    break
            else:
                time.sleep(poll_interval)
