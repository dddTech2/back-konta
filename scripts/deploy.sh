#!/usr/bin/env bash
# Despliegue de Konta en el VPS: actualiza back-konta y front-konta, compila la SPA, reconstruye los
# contenedores (el servicio `migrate` aplica las migraciones antes de que arranquen api/bot/scheduler)
# y verifica /health.
#
# Lo ejecuta GitHub Actions por SSH (workflows deploy.yml de ambos repos) o una persona a mano:
#     /opt/dianProyect/back-konta/scripts/deploy.sh            # despliega ambos repos
#     /opt/dianProyect/back-konta/scripts/deploy.sh front      # solo la SPA
#     /opt/dianProyect/back-konta/scripts/deploy.sh back       # solo backend
#
# Requisitos en el servidor (ver docs/DEPLOYMENT.md, sección "Despliegue automático"):
#   - El usuario que despliega es dueño de /opt/dianProyect (chown único con sudo) y está en el grupo docker.
#   - Node >= 18 y npm para compilar la SPA.
#   - El .env del backend ya existe: este script nunca lo crea ni lo modifica.
set -Eeuo pipefail

TARGET="${1:-all}"
BASE_DIR="${KONTA_BASE_DIR:-/opt/dianProyect}"
BACK_DIR="$BASE_DIR/back-konta"
FRONT_DIR="$BASE_DIR/front-konta"
BACK_BRANCH="${KONTA_BACK_BRANCH:-main}"
FRONT_BRANCH="${KONTA_FRONT_BRANCH:-master}"
HEALTH_URL="${KONTA_HEALTH_URL:-http://127.0.0.1:8020/health}"
LOCK_FILE="${KONTA_LOCK_FILE:-/tmp/konta-deploy.lock}"

log() { printf '[deploy %s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail() { log "ERROR: $*"; exit 1; }
trap 'fail "falló la línea $LINENO: $BASH_COMMAND"' ERR

case "$TARGET" in
  all|back|front) ;;
  *) fail "objetivo desconocido '$TARGET' (usa all, back o front)" ;;
esac

# Un solo despliegue a la vez: si los dos repos publican casi al mismo tiempo, el segundo espera.
exec 9>"$LOCK_FILE"
flock -w 900 9 || fail "otro despliegue sigue en curso después de 15 minutos"

# Lleva el repo exactamente a origin/<rama>. Los cambios locales en archivos versionados se descartan
# (el servidor no se edita a mano); los archivos ignorados como .env y dist/ no se tocan.
update_repo() {
  local dir="$1" branch="$2"
  [ -d "$dir/.git" ] || fail "no existe el repositorio $dir"
  [ -w "$dir/.git" ] || fail "$(whoami) no puede escribir en $dir (ver docs/DEPLOYMENT.md: chown único)"
  local before after
  before="$(git -C "$dir" rev-parse --short HEAD)"
  git -C "$dir" fetch --quiet origin "$branch"
  git -C "$dir" checkout --quiet "$branch"
  git -C "$dir" reset --quiet --hard "origin/$branch"
  after="$(git -C "$dir" rev-parse --short HEAD)"
  log "$(basename "$dir"): $before -> $after"
}

build_front() {
  log "Compilando la SPA"
  cd "$FRONT_DIR"
  npm ci --no-audit --no-fund
  # Se compila en una carpeta aparte: si el build falla, la SPA publicada sigue intacta.
  npx tsc --noEmit
  npx vite build --outDir dist.new --emptyOutDir
  # La API monta dist/ como volumen: se actualiza el CONTENIDO de la misma carpeta (no se reemplaza la carpeta,
  # o el contenedor seguiría viendo la vieja). Primero los assets nuevos (nombres con hash), luego index.html,
  # y al final se borran los assets que ya nadie referencia: así no hay un instante con index.html roto.
  mkdir -p dist/assets
  cp -a dist.new/assets/. dist/assets/
  find dist.new -mindepth 1 -maxdepth 1 ! -name assets -exec cp -a {} dist/ \;
  ( cd dist/assets && shopt -s nullglob && for f in *; do [ -e "../../dist.new/assets/$f" ] || rm -rf -- "$f"; done )
  rm -rf dist.new
}

deploy_back() {
  log "Reconstruyendo contenedores (migrate aplica las migraciones antes de api/bot/scheduler)"
  cd "$BACK_DIR"
  [ -f .env ] || fail "falta $BACK_DIR/.env"
  docker compose up -d --build --remove-orphans
  docker compose ps --format '{{.Service}}: {{.State}}'
}

check_health() {
  log "Verificando $HEALTH_URL"
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
      log "API sana"
      return 0
    fi
    sleep 2
  done
  cd "$BACK_DIR" && docker compose logs --tail 60 migrate api || true
  fail "la API no respondió en $HEALTH_URL"
}

log "Inicio del despliegue ($TARGET) como $(whoami)"
if [ "$TARGET" = all ] || [ "$TARGET" = front ]; then
  update_repo "$FRONT_DIR" "$FRONT_BRANCH"
  build_front
fi
if [ "$TARGET" = all ] || [ "$TARGET" = back ]; then
  update_repo "$BACK_DIR" "$BACK_BRANCH"
  deploy_back
fi
check_health
log "Despliegue terminado"
