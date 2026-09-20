#!/bin/sh
# Realiza copias de seguridad periódicas de la base de datos PostgreSQL usando pg_dump.
#
# Para restaurar una copia de seguridad:
#   pg_restore --clean --if-exists --no-owner -d <base> archivo.dump
# Ejemplo:
#   pg_restore --clean --if-exists --no-owner -h postgres -U kontable -d kontable /backups/kontable-20260920-150000.dump

set -eu

BACKUP_DIR=/backups
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-24}"

while true; do
  mkdir -p "$BACKUP_DIR"

  TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
  TARGET_FILE="$BACKUP_DIR/kontable-${TIMESTAMP}.dump"
  TMP_FILE="${TARGET_FILE}.tmp"

  if pg_dump --format=custom --no-owner -f "$TMP_FILE"; then
    mv "$TMP_FILE" "$TARGET_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Copia de seguridad completada con éxito: $TARGET_FILE"
    find "$BACKUP_DIR" -name 'kontable-*.dump' -mtime +"$KEEP_DAYS" -delete
    find "$BACKUP_DIR" -name '*.tmp' -delete
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: Falló el volcado con pg_dump hacia $TARGET_FILE" >&2
    rm -f "$TMP_FILE"
  fi

  sleep "$((INTERVAL_HOURS * 3600))"
done
