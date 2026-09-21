"""Worker remoto: corre en una red residencial (tu PC, o luego una Raspberry Pi) y ejecuta
las extracciones DIAN reales, mientras el bot + la API viven en el VPS.

Por qué existe: el VPS tiene IP de datacenter y la DIAN la bloquea a nivel de red
("Solicitud bloqueada por controles de seguridad") -- ver la conversación de despliegue.
Este script nunca toca la base de datos directamente; solo habla con 3 endpoints
internos de la API (protegidos con INTERNAL_WORKER_TOKEN) para tomar trabajos y reportar
resultados, y opcionalmente usa Redis (en el VPS) para reaccionar al instante en vez de
esperar su propio intervalo de polling.

Cada consulta a /internal/jobs/next es también su latido (cabecera X-Worker-Name): si deja de
consultar con trabajo pendiente, el servidor avisa. Si la subida del ZIP falla por red o por un
5xx, reintenta con espera creciente y, si no lo logra, deja el ZIP en pending_uploads/ para
reintentarlo en cada vuelta. Ver docs/DEPLOYMENT.md para instalarlo como tarea de inicio.

Uso:
    uv run kontable-worker-remote

Alternativa:
    python -m dian_automation.cli.worker_remote

Variables de entorno relevantes (ver .env):
    KONTABLE_API_URL       Base de la API en el VPS, ej. http://<ip-vps>:8020
    INTERNAL_WORKER_TOKEN  Mismo secreto configurado en el .env del VPS
    WORKER_NAME            Nombre en el latido (por defecto remote)
    PENDING_UPLOADS_DIR    Carpeta de ZIP por subir (por defecto pending_uploads/ en el directorio de trabajo)
    REDIS_URL              Opcional; redis://:password@<ip-vps>:6379/0 -- si no se
                            configura, el script sigue funcionando por polling puro.
"""

import os
import sys
import time
import shutil
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Callable
from dotenv import load_dotenv, find_dotenv
import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(find_dotenv(usecwd=True))

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
WORKER_NAME = (os.getenv("WORKER_NAME") or "remote").strip() or "remote"
POLL_INTERVAL_SECONDS = 10
# Empaquetado con PyInstaller, los ZIP pendientes van junto al .exe; en desarrollo van en el directorio de trabajo
_BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
PENDING_UPLOADS_DIR = Path(os.getenv("PENDING_UPLOADS_DIR") or _BASE_DIR / "pending_uploads")

# Subida del ZIP: espera 5 s que se duplica hasta 5 min, durante un máximo de 20 min
UPLOAD_TIMEOUT_SECONDS = 60.0
UPLOAD_RETRY_INITIAL_SECONDS = 5
UPLOAD_RETRY_MAX_WAIT_SECONDS = 300
UPLOAD_RETRY_BUDGET_SECONDS = 20 * 60

EVIDENCE_CANDIDATES = [
    "screenshot_turnstile_timeout.png",
    "screenshot_turnstile_widget.png",
    "screenshot_espera.png",
    "screenshot_tabla_reportes.png",
    "screenshot_antes_exportar.png",
]


class UploadRejected(Exception):
    """El servidor respondió 4xx a la subida del ZIP: reintentar no cambiará el resultado."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class UploadNotDelivered(Exception):
    """La subida no se logró dentro del tiempo permitido (red caída o 5xx sostenido)."""


def _auth_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {WORKER_TOKEN}"}


def fetch_next_job(client: httpx.Client) -> Optional[Dict[str, Any]]:
    headers = {**_auth_headers(), "X-Worker-Name": WORKER_NAME}
    res = client.get(f"{API_BASE}/internal/jobs/next", headers=headers, timeout=15.0)
    res.raise_for_status()
    return res.json().get("job")


def _error_detail(res: httpx.Response) -> str:
    try:
        detail = res.json().get("detail")
    except Exception:
        detail = None
    return str(detail or res.text or "sin detalle")[:500]


def _upload_zip_once(client: httpx.Client, job_id: str, zip_path: str) -> Dict[str, Any]:
    """Una sola petición. 2xx devuelve el JSON; 4xx lanza UploadRejected; red o 5xx lanzan httpx.HTTPError."""
    with open(zip_path, "rb") as f:
        res = client.post(
            f"{API_BASE}/internal/jobs/{job_id}/complete",
            headers=_auth_headers(),
            files={"file": (os.path.basename(zip_path), f, "application/zip")},
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
    if 400 <= res.status_code < 500:
        raise UploadRejected(res.status_code, _error_detail(res))
    res.raise_for_status()
    return res.json()


def upload_with_retry(
    client: httpx.Client,
    job_id: str,
    zip_path: str,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    """Sube el ZIP reintentando solo ante error de red o 5xx, con espera creciente y tope de tiempo total."""
    deadline = monotonic() + UPLOAD_RETRY_BUDGET_SECONDS
    wait = UPLOAD_RETRY_INITIAL_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            return _upload_zip_once(client, job_id, zip_path)
        except httpx.HTTPError as e:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise UploadNotDelivered(f"{attempt} intentos sin éxito: {e}") from e
            pause = min(wait, remaining)
            logger.warning(f"Subida del job {job_id} falló (intento {attempt}): {e}. Reintento en {pause:.0f} s.")
            sleep(pause)
            wait = min(wait * 2, UPLOAD_RETRY_MAX_WAIT_SECONDS)


def park_zip(zip_path: str, job_id: str) -> Path:
    """Guarda el ZIP en pending_uploads/{job_id}.zip para reintentar su subida más tarde."""
    PENDING_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = PENDING_UPLOADS_DIR / f"{job_id}.zip"
    dest.unlink(missing_ok=True)
    shutil.move(str(zip_path), str(dest))
    return dest


def retry_pending_uploads(client: httpx.Client) -> None:
    """Un intento por cada ZIP de pending_uploads/. Si la API no responde, corta la pasada."""
    if not PENDING_UPLOADS_DIR.is_dir():
        return
    for path in sorted(PENDING_UPLOADS_DIR.glob("*.zip")):
        job_id = path.stem
        try:
            result = _upload_zip_once(client, job_id, str(path))
        except UploadRejected as e:
            rejected = path.with_name(path.name + ".rejected")
            path.replace(rejected)
            logger.error(f"El servidor rechazó el ZIP pendiente del job {job_id} ({e}); queda en {rejected.name}.")
            continue
        except httpx.HTTPError as e:
            logger.warning(f"No se pudo subir el ZIP pendiente del job {job_id}: {e}. Se reintenta en la próxima vuelta.")
            return
        path.unlink()
        if result.get("ignored"):
            logger.info(f"ZIP pendiente del job {job_id} descartado: el job ya estaba completado.")
        else:
            logger.info(f"ZIP pendiente del job {job_id} subido. Respuesta: {result}")


def report_success(client: httpx.Client, job_id: str, zip_path: str) -> None:
    try:
        result = upload_with_retry(client, job_id, zip_path)
    except UploadRejected as e:
        # 4xx (ej. 422 ZIP inválido): no se reintenta; falla dura
        logger.error(f"El servidor rechazó el ZIP del job {job_id}: {e}")
        report_failure(client, job_id, error_code="UploadRejected", error_detail=f"El servidor rechazó el ZIP ({e}).")
        return
    except UploadNotDelivered as e:
        parked = park_zip(zip_path, job_id)
        logger.error(f"No se pudo subir el ZIP del job {job_id} ({e}). Guardado en {parked} para reintentarlo.")
        return
    logger.info(f"Job {job_id} reportado como exitoso. Respuesta: {result}")


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


def worker_loop(
    client: httpx.Client,
    max_iterations: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
    wait_signal: Callable[..., Any] = wait_for_job_signal,
) -> None:
    """Bucle principal: nunca termina por una excepción (solo Ctrl+C lo detiene)."""
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            retry_pending_uploads(client)
            job = fetch_next_job(client)
            if job:
                process_job(client, job)
                continue  # revisar de inmediato si hay otro pendiente
        except httpx.HTTPError as e:
            logger.warning(f"No se pudo consultar la API ({API_BASE}): {e}")
        except Exception:
            logger.exception(f"Error inesperado en el bucle del worker; se sigue en {POLL_INTERVAL_SECONDS} s.")
            sleep(POLL_INTERVAL_SECONDS)
            continue

        # Sin trabajo pendiente: esperar la señal de Redis (o el intervalo de polling)
        wait_signal(timeout_seconds=POLL_INTERVAL_SECONDS) or sleep(1)


def main():
    if not WORKER_TOKEN:
        logger.error("INTERNAL_WORKER_TOKEN no configurado. Configúralo igual en el .env del VPS y aquí.")
        if getattr(sys, "frozen", False):
            logger.error(f"Crea un archivo .env con KONTABLE_API_URL e INTERNAL_WORKER_TOKEN en: {Path.cwd()}")
            if sys.stdin is not None and sys.stdin.isatty():
                input("Presiona Enter para cerrar...")  # con doble clic la ventana se cerraría antes de poder leer
        return

    logger.info(f"Worker remoto '{WORKER_NAME}' iniciado. API destino: {API_BASE}. Ctrl+C para detener.")
    with httpx.Client() as client:
        worker_loop(client)


def run() -> None:
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Worker remoto detenido por el usuario.")


if __name__ == "__main__":
    run()
