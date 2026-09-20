"""Programador semanal de extracciones DIAN.

Corre en el SERVIDOR, en un proceso aparte del bot, la API y el worker. No abre la DIAN: solo
encola trabajos que el worker (local o remoto) procesa después. Cada minuto revisa la hora de
Bogotá; el domingo a las 03:00 encola el mes en curso de cada negocio activo y, los días 1 a 5 del
mes, el cierre del mes anterior.

Nace apagado: mientras SCHEDULER_ENABLED no sea 'true' solo registra que está deshabilitado.

Uso:
    uv run python run_scheduler.py

Variables de entorno relevantes (ver .env.example):
    SCHEDULER_ENABLED   'true' para encolar; cualquier otro valor lo deja apagado (por defecto false).
    SCHEDULER_WEEKDAY   Día de la corrida semanal, 0 = lunes ... 6 = domingo (por defecto 6).
    SCHEDULER_HOUR      Hora de Bogotá de la corrida, 0 a 23 (por defecto 3).
    DATABASE_URL        Opcional; por defecto sqlite:///./kontable.db.
"""

import sys
import logging
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from dian_automation.config import config
from dian_automation.db.database import SessionLocal
from dian_automation.queue.scheduler import ExtractionScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scheduler_runner")


def main():
    try:
        scheduler = ExtractionScheduler(
            db_session_factory=SessionLocal,
            weekday=config.scheduler_weekday,
            hour=config.scheduler_hour,
        )
    except ValueError as e:
        logger.error(f"Configuración inválida del programador: {e}")
        sys.exit(1)

    logger.info("Programador de extracciones DIAN iniciado (ciclo cada 60 s). Ctrl+C para detener.")
    try:
        scheduler.run_loop(enabled=config.scheduler_enabled, interval=60)
    except KeyboardInterrupt:
        logger.info("Programador detenido por el usuario.")


if __name__ == "__main__":
    main()
