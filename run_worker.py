"""Worker persistente de extracciones DIAN.

Corre en un PROCESO APARTE del bot de Telegram (run_telegram_bot.py). Mientras el bot
solo encola trabajos (comando /ejecutar_extraccion de la administradora), este script
es el que realmente los procesa: uno a la vez, en orden, llamando a dian_flow.run_flow()
para descargar el listado real desde el portal DIAN VPFE y parseándolo hacia
Invoice / MonthlyTaxSummary. Si una extracción falla, notifica automáticamente a los
usuarios TECH_OPS por Telegram (captura de pantalla + código de error + botón de reintento).

Uso:
    uv run python run_worker.py

Variables de entorno relevantes (ver .env):
    TELEGRAM_BOT_TOKEN   Token del bot único (usado también para alertar a Tech Ops).
    DATABASE_URL         Opcional; por defecto sqlite:///./kontable.db.
"""

import os
import sys
import time
import logging
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from dian_automation.db.database import init_db, SessionLocal
from dian_automation.queue.worker import ExtractionWorker
from dian_automation.telegram.tech_ops_bot import TechOpsAlertBot, create_tech_ops_on_failure_callback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("worker_runner")


def main():
    init_db()

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TECH_OPS_BOT_TOKEN")
    on_failure_callback = None
    if bot_token:
        tech_ops_bot = TechOpsAlertBot(bot_token=bot_token)
        on_failure_callback = create_tech_ops_on_failure_callback(
            bot=tech_ops_bot,
            db_session_factory=SessionLocal,
        )
        logger.info("Alertas a Tech Ops activas (se notificará por Telegram ante cualquier fallo).")
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN no configurado: los fallos de extracción NO notificarán a Tech Ops."
        )

    worker = ExtractionWorker(db_session_factory=SessionLocal)
    logger.info("Worker de extracción DIAN iniciado (concurrencia 1, sondeo cada 5s). Ctrl+C para detener.")

    try:
        worker.run_loop(poll_interval=5, on_failure_callback=on_failure_callback)
    except KeyboardInterrupt:
        logger.info("Worker detenido por el usuario.")


if __name__ == "__main__":
    main()
