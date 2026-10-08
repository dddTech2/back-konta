"""Programador semanal de extracciones DIAN y alertas de vencimientos tributarios.

Corre en el SERVIDOR, en un proceso aparte del bot, la API y el worker. No abre la DIAN: solo
encola trabajos que el worker (local o remoto) procesa después. Cada minuto revisa la hora de
Bogotá; el domingo a las 03:00 encola el mes en curso de cada negocio activo y, los días 1 a 5 del
mes, el cierre del mes anterior.

A las 08:00 (o la hora configurada en TAX_ALERTS_HOUR) revisa las obligaciones tributarias
próximas o que vencen el día de hoy y envía avisos proactivos por Telegram a los contribuyentes
habilitados (Story 4.1c).

Nace apagado para extracciones: mientras SCHEDULER_ENABLED no sea 'true' solo registra que está
deshabilitado. Con o sin encolar, en cada ciclo vigila el latido del worker remoto y, si
TAX_ALERTS_ENABLED es 'true', evalúa y envía las alertas tributarias en su ventana horaria.

Uso:
    uv run kontable-scheduler

Alternativa:
    python -m dian_automation.cli.scheduler

Variables de entorno relevantes (ver .env.example):
    SCHEDULER_ENABLED       'true' para encolar; cualquier otro valor lo deja apagado (por defecto false).
    SCHEDULER_WEEKDAY       Día de la corrida semanal, 0 = lunes ... 6 = domingo (por defecto 6).
    SCHEDULER_HOUR          Hora de Bogotá de la corrida de extracciones, 0 a 23 (por defecto 3).
    WORKER_SILENCE_MINUTES  Minutos sin latido del worker antes de avisar (por defecto 15).
    TAX_ALERTS_ENABLED      'true' para enviar alertas tributarias por Telegram (por defecto true).
    TAX_ALERTS_HOUR         Hora de Bogotá para alertas tributarias, 0 a 23 (por defecto 8).
    DATABASE_URL            Opcional; por defecto sqlite:///./kontable.db.
"""

import sys
import logging
from dotenv import load_dotenv, find_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(find_dotenv(usecwd=True))

from dian_automation.config import config
from dian_automation.core.calendar_engine import today_bogota
from dian_automation.core.tax_alerts import TaxDeadlineAlerter
from dian_automation.db.database import SessionLocal
from dian_automation.queue.scheduler import ExtractionScheduler, _to_bogota
from dian_automation.queue.worker_heartbeat import WorkerSilenceMonitor
from dian_automation.telegram.admin_alerts import notify_tech_ops
from dian_automation.telegram.notify import send_telegram_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scheduler_runner")


def main():
    try:
        monitor = WorkerSilenceMonitor(SessionLocal, silence_minutes=config.worker_silence_minutes)
        tax_alerts_callback = None
        tax_alerts_enabled = getattr(config, "tax_alerts_enabled", True)
        tax_alerts_hour = getattr(config, "tax_alerts_hour", 8)
        calendar_upcoming_days = getattr(config, "calendar_upcoming_days", 15)

        if tax_alerts_enabled:
            alerter = TaxDeadlineAlerter(
                db_session_factory=SessionLocal,
                sender=send_telegram_message,
                upcoming_days=calendar_upcoming_days,
                tech_ops_notifier=notify_tech_ops,
            )
            tax_alerts_callback = lambda now=None: alerter.run(
                today=_to_bogota(now).date() if now is not None else today_bogota()
            )

        scheduler = ExtractionScheduler(
            db_session_factory=SessionLocal,
            weekday=config.scheduler_weekday,
            hour=config.scheduler_hour,
            worker_watch=monitor.check,
            tax_alerts=tax_alerts_callback,
            tax_alerts_hour=tax_alerts_hour,
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
