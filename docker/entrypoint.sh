#!/bin/sh
# Punto de entrada compartido por los servicios. El nombre del proceso llega como
# primer argumento (ver `command:` de cada servicio en docker-compose.yml).
set -e

mkdir -p /data
export DATABASE_URL="${DATABASE_URL:-sqlite:////data/kontable.db}"
export DOWNLOAD_DIR="${DOWNLOAD_DIR:-/data/downloads}"
export ARTIFACTS_DIR="${ARTIFACTS_DIR:-/data/artifacts}"
mkdir -p "$DOWNLOAD_DIR" "$ARTIFACTS_DIR"

case "$1" in
  migrate)
    echo "[entrypoint] Aplicando migraciones (alembic upgrade head)..."
    exec alembic upgrade head
    ;;
  api)
    echo "[entrypoint] Iniciando API FastAPI (uvicorn) en 0.0.0.0:8000..."
    exec uvicorn dian_automation.api.app:app --host 0.0.0.0 --port 8000
    ;;
  bot)
    echo "[entrypoint] Iniciando bot de Telegram (long polling)..."
    exec python run_telegram_bot.py
    ;;
  scheduler)
    echo "[entrypoint] Iniciando programador semanal de descargas (SCHEDULER_ENABLED=${SCHEDULER_ENABLED:-false})..."
    exec python run_scheduler.py
    ;;
  *)
    exec "$@"
    ;;
esac
