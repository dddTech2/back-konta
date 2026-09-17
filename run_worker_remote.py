"""Worker remoto: corre en una red residencial (tu PC, o luego una Raspberry Pi) y ejecuta
las extracciones DIAN reales, mientras el bot + la API viven en el VPS.

Por qué existe: el VPS tiene IP de datacenter y la DIAN la bloquea a nivel de red
("Solicitud bloqueada por controles de seguridad") -- ver la conversación de despliegue.
Este script nunca toca la base de datos directamente; solo habla con 3 endpoints
internos de la API (protegidos con INTERNAL_WORKER_TOKEN) para tomar trabajos y reportar
resultados, y opcionalmente usa Redis (en el VPS) para reaccionar al instante en vez de
esperar su propio intervalo de polling.

Uso:
    uv run python run_worker_remote.py

Variables de entorno relevantes (ver .env):
    KONTABLE_API_URL       Base de la API en el VPS, ej. http://<ip-vps>:8020
    INTERNAL_WORKER_TOKEN  Mismo secreto configurado en el .env del VPS
    REDIS_URL              Opcional; redis://:password@<ip-vps>:6379/0 -- si no se
                            configura, el script sigue funcionando por polling puro.
"""

import os
import sys
import time
import asyncio
import logging
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from dian_automation.dian_flow import run_flow
from dian_automation.queue.redis_signal import wait_for_job_signal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("worker_remote")

API_BASE = os.getenv("KONTABLE_API_URL", "http://127.0.0.1:8000").rstrip("/")
WORKER_TOKEN = os.getenv("INTERNAL_WORKER_TOKEN", "")
POLL_INTERVAL_SECONDS = 10

EVIDENCE_CANDIDATES = [
    "screenshot_turnstile_timeout.png",
    "screenshot_turnstile_widget.png",
    "screenshot_espera.png",
    "screenshot_tabla_reportes.png",
    "screenshot_antes_exportar.png",
]


def _auth_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {WORKER_TOKEN}"}


def fetch_next_job(client: httpx.Client) -> Optional[Dict[str, Any]]:
    res = client.get(f"{API_BASE}/internal/jobs/next", headers=_auth_headers(), timeout=15.0)
    res.raise_for_status()
    return res.json().get("job")


def report_success(client: httpx.Client, job_id: str, zip_path: str) -> None:
    with open(zip_path, "rb") as f:
        res = client.post(
            f"{API_BASE}/internal/jobs/{job_id}/complete",
            headers=_auth_headers(),
            files={"file": (os.path.basename(zip_path), f, "application/zip")},
            timeout=60.0,
        )
    res.raise_for_status()
    logger.info(f"Job {job_id} reportado como exitoso. Respuesta: {res.json()}")


def report_failure(client: httpx.Client, job_id: str, error_code: str, error_detail: str) -> None:
    files = {}
    screenshot_path = next((p for p in EVIDENCE_CANDIDATES if os.path.exists(p)), None)
    opened_file = None
    try:
        if screenshot_path:
            opened_file = open(screenshot_path, "rb")
            files["screenshot"] = (os.path.basename(screenshot_path), opened_file, "image/png")
        res = client.post(
            f"{API_BASE}/internal/jobs/{job_id}/fail",
            headers=_auth_headers(),
            data={"error_code": error_code, "error_detail": error_detail[:2000]},
            files=files or None,
            timeout=30.0,
        )
        res.raise_for_status()
        logger.info(f"Job {job_id} reportado como fallido. Respuesta: {res.json()}")
    finally:
        if opened_file:
            opened_file.close()


def process_job(client: httpx.Client, job: Dict[str, Any]) -> None:
    job_id = job["job_id"]
    logger.info(f"Procesando job {job_id} (negocio {job['business_id']}, periodo {job['target_period']})...")
    try:
        zip_path = asyncio.run(
            run_flow(
                target_month=job["target_period"],
                login_type=job["login_type"],
                representative_code=job.get("representative_code"),
                company_nit=job.get("company_nit"),
                person_code=job.get("person_code"),
            )
        )
        report_success(client, job_id, zip_path)
    except Exception as e:
        logger.error(f"Fallo en job {job_id}: {e}", exc_info=True)
        error_code = type(e).__name__
        report_failure(client, job_id, error_code=error_code, error_detail=str(e))


def main():
    if not WORKER_TOKEN:
        logger.error("INTERNAL_WORKER_TOKEN no configurado. Configúralo igual en el .env del VPS y aquí.")
        return

    logger.info(f"Worker remoto iniciado. API destino: {API_BASE}. Ctrl+C para detener.")
    with httpx.Client() as client:
        while True:
            try:
                job = fetch_next_job(client)
                if job:
                    process_job(client, job)
                    continue  # revisar de inmediato si hay otro pendiente
            except httpx.HTTPError as e:
                logger.warning(f"No se pudo consultar la API ({API_BASE}): {e}")

            # Sin trabajo pendiente: esperar la señal de Redis (o el intervalo de polling)
            wait_for_job_signal(timeout_seconds=POLL_INTERVAL_SECONDS) or time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Worker remoto detenido por el usuario.")
