"""Endpoints internos para el worker remoto (ej. corriendo en una red residencial,
fuera del VPS cuya IP la DIAN bloquea).

Diseño: la base de datos (SQLite/Postgres) NUNCA se expone directamente a internet.
El worker remoto solo habla con estos 3 endpoints, protegidos con un token compartido
(INTERNAL_WORKER_TOKEN) via header `Authorization: Bearer <token>`:
  - GET  /internal/jobs/next            -> toma y reserva el siguiente job pendiente
  - POST /internal/jobs/{job_id}/complete -> sube el ZIP descargado; el servidor lo parsea
  - POST /internal/jobs/{job_id}/fail      -> reporta un fallo (con captura opcional)

Todas las escrituras a la base de datos (incluida la ingesta del XLSX) las sigue haciendo
únicamente el proceso de la API en el VPS, igual que hace el worker local en worker.py.
"""

import logging
import os
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Header, status
from sqlalchemy.orm import Session

from dian_automation.config import config
from dian_automation.db.database import get_db, SessionLocal
from dian_automation.db.models import DIANExtractionJob, Business
from dian_automation.queue.exceptions import STALE_PROCESSING_CODE
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.queue.worker_heartbeat import normalize_worker_name, record_heartbeat
from dian_automation.extraction.xlsx_parser import DIANXLSXParser, DIANParseError
from dian_automation.telegram.tech_ops_bot import TechOpsAlertBot
from dian_automation.telegram.admin_alerts import create_failure_alert_callback

logger = logging.getLogger("internal_worker_api")

router = APIRouter(prefix="/internal/jobs", tags=["Internal Worker"])


def require_worker_token(authorization: Optional[str] = Header(default=None)) -> None:
    """Valida el header `Authorization: Bearer <INTERNAL_WORKER_TOKEN>`."""
    if not config.internal_worker_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="INTERNAL_WORKER_TOKEN no está configurado en el servidor.",
        )
    expected = f"Bearer {config.internal_worker_token}"
    if not authorization or authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token de worker inválido.")


def _notify_failure(job: DIANExtractionJob, error_code: str, error_detail: str, screenshot_path: Optional[str]) -> None:
    """Avisa por Telegram según la clase del fallo (ADMIN si es lento, TECH_OPS si es duro o agotado).
    Sin token de bot no se avisa; un error de Telegram nunca afecta al registro del fallo."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TECH_OPS_BOT_TOKEN")
    if not bot_token:
        return
    callback = create_failure_alert_callback(bot=TechOpsAlertBot(bot_token=bot_token), db_session_factory=SessionLocal)
    try:
        callback(job, error_code, error_detail, screenshot_path)
    except Exception as e:
        logger.error(f"Error notificando el fallo remoto del job {job.id}: {e}")


@router.get("/next", dependencies=[Depends(require_worker_token)])
def get_next_job(db: Session = Depends(get_db), x_worker_name: Optional[str] = Header(default=None)):
    """Toma y reserva (PROCESSING) el siguiente job pendiente, con todo lo que el
    worker remoto necesita para llamar a dian_flow.run_flow() sin tocar la base de datos.
    Registra el latido del worker (X-Worker-Name) y libera los trabajos atascados en PROCESSING
    para que la cola vuelva a avanzar."""
    try:
        record_heartbeat(db, normalize_worker_name(x_worker_name))
    except Exception:
        db.rollback()
        logger.exception("No se pudo guardar el latido del worker; se sigue entregando trabajos.")

    for stale_job in ExtractionQueueManager.recover_stale_jobs(db=db):
        logger.warning(f"Job {stale_job.id} atascado en PROCESSING: reprogramado como fallo lento.")
        _notify_failure(stale_job, STALE_PROCESSING_CODE, stale_job.error_detail, None)

    job = ExtractionQueueManager.get_next_runnable_job(db=db)
    if not job:
        return {"job": None}

    business = db.query(Business).filter(Business.id == job.business_id).first()
    if not business:
        raise HTTPException(status_code=500, detail=f"Negocio '{job.business_id}' no encontrado para el job {job.id}")

    job = ExtractionQueueManager.mark_job_processing(job_id=job.id, db=db)

    login_type = "empresa" if business.taxpayer_type == "PERSONA_JURIDICA" else "persona"
    return {
        "job": {
            "job_id": job.id,
            "business_id": business.id,
            "target_period": job.target_period,
            "login_type": login_type,
            "representative_code": business.legal_rep_doc if login_type == "empresa" else None,
            "company_nit": business.nit if login_type == "empresa" else None,
            "person_code": business.nit if login_type == "persona" else None,
        }
    }


@router.post("/{job_id}/complete", dependencies=[Depends(require_worker_token)])
async def complete_job(job_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Recibe el ZIP descargado por el worker remoto, lo guarda y lo parsea (ingesta).
    Acepta también un job ya recuperado como atascado (ENQUEUED): la ingesta es idempotente por CUFE."""
    job = db.query(DIANExtractionJob).filter(DIANExtractionJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' no encontrado")

    if job.status == "SUCCESS":
        # Subida repetida (ej. un ZIP que quedó en pending_uploads/ del worker): no se ingiere de nuevo ni se re-espacia la cola
        logger.info(f"ZIP repetido ignorado: el job {job_id} ya está SUCCESS.")
        return {"status": "SUCCESS", "job_id": job_id, "ignored": True}

    download_dir = os.getenv("DOWNLOAD_DIR", "./downloads")
    os.makedirs(download_dir, exist_ok=True)
    zip_path = os.path.join(download_dir, f"{job_id}.zip")
    contents = await file.read()
    with open(zip_path, "wb") as f:
        f.write(contents)

    try:
        parse_result = DIANXLSXParser.parse_zip(zip_path=zip_path, business_id=job.business_id, db=db, job_id=job.id)
    except DIANParseError as e:
        raise HTTPException(status_code=422, detail=f"El ZIP se recibió pero no se pudo parsear: {e}")

    job = ExtractionQueueManager.mark_job_success(job_id=job_id, zip_path=zip_path, db=db)
    logger.info(f"Job {job_id} completado por worker remoto. Ingesta: {parse_result}")
    return {"status": "SUCCESS", "job_id": job_id, "parse_result": parse_result}


@router.post("/{job_id}/fail", dependencies=[Depends(require_worker_token)])
async def fail_job(
    job_id: str,
    error_code: str = Form(...),
    error_detail: str = Form(...),
    screenshot: Optional[UploadFile] = File(default=None),
    db: Session = Depends(get_db),
):
    """Reporta el fallo de un job procesado remotamente (con captura de pantalla opcional)."""
    existing = db.query(DIANExtractionJob).filter(DIANExtractionJob.id == job_id).first()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' no encontrado")

    if existing.status != "PROCESSING":
        # Fallo tardío de un job ya recuperado o cerrado: no debe reprogramarlo ni contar otro intento
        logger.info(f"Fallo tardío ignorado: el job {job_id} está {existing.status}, no PROCESSING.")
        return {
            "status": existing.status,
            "job_id": job_id,
            "attempt_count": existing.attempt_count,
            "ignored": True,
        }

    screenshot_path = None
    if screenshot is not None:
        download_dir = os.getenv("DOWNLOAD_DIR", "./downloads")
        os.makedirs(download_dir, exist_ok=True)
        screenshot_path = os.path.join(download_dir, f"evidence_{job_id}.png")
        contents = await screenshot.read()
        with open(screenshot_path, "wb") as f:
            f.write(contents)

    job = ExtractionQueueManager.mark_job_failed(
        job_id=job_id,
        error_code=error_code,
        error_detail=error_detail,
        screenshot_path=screenshot_path,
        db=db,
    )

    _notify_failure(job, error_code, error_detail, screenshot_path)

    return {
        "status": "FAILED" if job.status == "FAILED" else "RETRY_SCHEDULED",
        "job_id": job_id,
        "attempt_count": job.attempt_count,
    }
