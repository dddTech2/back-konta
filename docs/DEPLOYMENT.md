# Despliegue del worker remoto

El servidor (API, bot, `scheduler`, Redis) corre en un VPS Linux cuya IP la DIAN bloquea. Las descargas las hace el **worker remoto** (`uv run kontable-worker-remote`) desde una red residencial. Este documento cubre el equipo del worker; el servidor se levanta con `docker-compose.yml`.

```
VPS  ── API + bot + scheduler + Redis ──┐
                                        │  HTTPS  /internal/jobs/*   (token compartido)
Casa ── worker remoto + Chrome ─────────┘  el worker nunca toca la base de datos
```

- El `scheduler` (servidor) **solo encola**: el domingo a las 03:00 (hora de Bogotá) el mes en curso y, los días 1 a 5, el cierre del mes anterior.
- El worker consulta `GET /internal/jobs/next`; cada consulta es su **latido**. La cola entrega un trabajo a la vez y espera 15 minutos tras cada éxito, así que con 8 clientes la corrida semanal dura unas 2 a 3 horas.
- Si el equipo del worker está apagado a las 03:00, los trabajos esperan en la cola hasta que arranque.

## 1. Requisitos del equipo del worker

| Requisito | Detalle |
|---|---|
| Red | Residencial (no un datacenter) y con salida a internet hacia la API del VPS. |
| Sistema | Windows 10/11, o Linux/Raspberry Pi con escritorio o `xvfb`. |
| Python y `uv` | Python 3.14 o superior (`requires-python` del proyecto) y [uv](https://docs.astral.sh/uv/). |
| Chrome | Google Chrome o Chromium instalado. Si no está en una ruta habitual, define `CHROME_PATH`. |
| Sesión de escritorio | Con `HEADLESS=False` (recomendado contra Turnstile) Chrome abre ventana, así que el worker necesita una sesión iniciada. |
| Código | Copia de `ProyectoDianBack` con `uv sync` ejecutado. |

## 2. Configuración (`.env` del worker)

Copia `.env.example` a `.env` y define, como mínimo:

| Variable | Para qué |
|---|---|
| `KONTABLE_API_URL` | URL pública de la API en el VPS, ej. `http://<ip-o-dominio>:8020`. |
| `INTERNAL_WORKER_TOKEN` | El mismo secreto que en el `.env` del VPS. Sin él el worker no arranca. |
| `STALWART_IMAP_HOST`, `STALWART_IMAP_PORT`, `STALWART_USER`, `STALWART_PASSWORD` | Buzón donde llega el enlace mágico de la DIAN. |
| `REDIS_URL` | Opcional: `redis://:<clave>@<ip-vps>:6390/0`. Despierta al worker al instante; sin él consulta cada 10 s. |
| `HEADLESS` | `False` recomendado. |
| `WORKER_NAME` | Opcional, nombre en el latido (por defecto `remote`). Útil si algún día hay dos equipos. |
| `PENDING_UPLOADS_DIR` | Opcional, carpeta de ZIP por subir (por defecto `pending_uploads/` junto al script). |

Las credenciales de la DIAN **no** viven en el worker: llegan con cada trabajo desde la API.

Prueba a mano antes de automatizar:

```bash
uv run kontable-worker-remote
```

Debe imprimir `Worker remoto 'remote' iniciado` y, sin trabajos, quedar esperando.

## 3. Arranque automático

### Windows (tarea programada)

Desde la carpeta del proyecto, en PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\install_worker_task.ps1 -StartNow
```

Registra la tarea **"Kontable Worker"**: arranca al iniciar sesión el usuario actual, se reinicia cada minuto si el worker termina con error y guarda su registro en `worker.log`. Para quitarla:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\install_worker_task.ps1 -Uninstall
```

Si el equipo es dedicado, activa el inicio de sesión automático (`netplwiz`) y desactiva la suspensión, para que el worker vuelva solo tras un corte de luz. No es un servicio de Windows a propósito: un servicio no tiene sesión de escritorio y Chrome no podría abrir ventana. Pasar a servicio exige `HEADLESS=True` y comprobar que Turnstile lo acepta.

### Windows sin Python (ejecutable)

`packaging/kontable_worker.spec` empaqueta el worker con PyInstaller para un equipo sin Python ni `uv`. En el equipo de desarrollo, desde la raíz del proyecto:

```powershell
uv run --with pyinstaller pyinstaller packaging/kontable_worker.spec --noconfirm
```

Genera la carpeta resultante en `dist/kontable-worker/` de la raíz del proyecto (unos 130 MB), que se copia entera al equipo del worker. Ahí se coloca el `.env` (sección 2) y se lanza `kontable-worker.exe` con esa carpeta como directorio de trabajo: `.env`, `downloads\`, `.browser_profile\` y `pending_uploads\` quedan junto al ejecutable. Chrome debe estar instalado en el equipo; no se empaqueta. `scripts\ops\install_worker_task.ps1` sigue lanzando el worker con `uv`; para arrancar el `.exe` al iniciar sesión hay que crear la tarea aparte.

### Linux / Raspberry Pi (`systemd`)

Ejemplo de `/etc/systemd/system/kontable-worker.service` (ajusta usuario y rutas). Con escritorio, agrega `Environment=DISPLAY=:0`; sin escritorio, ejecuta el comando con `xvfb-run -a`.

```ini
[Unit]
Description=Kontable worker remoto
After=network-online.target
Wants=network-online.target

[Service]
User=kontable
WorkingDirectory=/opt/kontable/ProyectoDianBack
ExecStart=/usr/bin/env uv run kontable-worker-remote
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kontable-worker
journalctl -u kontable-worker -f
```

## 4. Subidas que fallan

Si la red cae justo al subir el ZIP, el worker lo conserva y reintenta con espera creciente (5 s hasta 5 min) durante un máximo de 20 minutos. Si no lo logra, lo deja en `pending_uploads/{job_id}.zip` y lo reintenta al iniciar y en cada vuelta. Un rechazo 4xx (por ejemplo 422, ZIP inválido) no se reintenta: se reporta como fallo duro, y un ZIP pendiente rechazado queda como `*.zip.rejected` para revisarlo. Si el trabajo ya estaba completado, la API lo ignora y el archivo se borra.

## 5. Verificar el latido

En el equipo del worker: `worker.log` (Windows) o `journalctl` (Linux) muestra `Procesando job ...` cuando hay trabajo y ningún error de conexión.

En el servidor, la tabla `worker_heartbeats` guarda la última consulta (hora del servidor, UTC):

```bash
docker compose exec api python -c "from dian_automation.db.database import SessionLocal; from dian_automation.db.models import WorkerHeartbeat as W; db = SessionLocal(); print([(w.name, w.last_seen_at) for w in db.query(W).all()])"
```

`last_seen_at` debe tener menos de un minuto de antigüedad mientras el worker corre.

## 6. Si llega el aviso «El worker de descargas no responde»

El proceso `scheduler` lo envía por Telegram a TECH_OPS y a la administradora cuando pasan más de `WORKER_SILENCE_MINUTES` (15) sin latido y hay trabajos listos. Funciona aunque `SCHEDULER_ENABLED` sea `false`, y no se repite antes de 1 hora.

1. ¿El equipo está encendido, con sesión iniciada e internet? En Windows revisa que la tarea "Kontable Worker" esté en ejecución (Programador de tareas).
2. Mira el final de `worker.log`: un error de conexión apunta a la red o a `KONTABLE_API_URL`; un 401, a un `INTERNAL_WORKER_TOKEN` distinto.
3. Reinicia el worker (o la tarea). Al consultar de nuevo, el latido se reanuda y los trabajos pendientes continúan solos.
4. Si había un ZIP en `pending_uploads/`, se sube en la primera vuelta.

Un trabajo que quedó en `PROCESSING` sin respuesta se reprograma solo a las 6 horas (Story 1.6).

## 7. Activar la programación semanal

`SCHEDULER_ENABLED` nace en `false`. Actívalo (`true` en el `.env` del VPS y reinicia el servicio `scheduler`) solo cuando el worker arranque solo, el latido se vea en `worker_heartbeats` y hayas probado un reinicio del equipo.

## 8. Calendario tributario del año siguiente

El motor de calendario (`GET /api/calendar`, `/vencimientos` del bot, el pill de vencimiento del Dashboard y las fechas de `/api/iva`) busca las fechas del año en curso (hora de Bogotá). Si no hay ninguna fila de `dian_tax_calendar` para ese año, falla a propósito: la API responde 409, el bot avisa que el calendario no está cargado y la SPA muestra "Calendario no disponible". Para que eso no ocurra el 1 de enero:

1. Antes del 1 de enero, consigue el calendario oficial del año siguiente y arma `data/calendario_dian_AAAA.csv` con el mismo formato que `data/calendario_dian_2026.csv`.
2. Cárgalo en el servidor con `uv run python -m dian_automation.core.calendar_loader data/calendario_dian_AAAA.csv`: valida el archivo completo y, si algo falla, lo rechaza entero y deja la base intacta.
3. Verifica con `GET /api/calendar/<business_id>` de un negocio `DIAN` que responde 200.

## 9. Levantar el servidor (Docker)

En el VPS, con el repositorio `front-konta` como carpeta hermana de `back-konta`/`ProyectoDianBack`:

1. `cp .env.example .env` y completa al menos:
   - `POSTGRES_PASSWORD`: contraseña de PostgreSQL (usa solo letras y números para que la URL de conexión no requiera escapes de caracteres, ej. `openssl rand -hex 24`).
   - `REDIS_PASSWORD`: contraseña segura para el servidor Redis.
   - `TELEGRAM_BOT_TOKEN`, `ADMIN_BOOTSTRAP_CHAT_IDS`, `JWT_SECRET`, `INTERNAL_WORKER_TOKEN`.
2. Compila la SPA, que la API sirve desde `../front-konta/dist`: `cd ../front-konta && npm ci && npm run build`.
3. `docker compose up -d --build`.
   - **Orden de arranque y dependencias:** el contenedor `postgres` inicia primero y ejecuta su comprobación de salud (`pg_isready`). Una vez sano, se ejecuta el servicio de un solo uso `migrate` (`alembic upgrade head`) para crear o actualizar el esquema. Tras completarse la migración con éxito, arrancan los servicios `api`, `bot`, `scheduler` y `backup` (en Docker el bot y el programador se lanzan con `python -m dian_automation.cli.telegram_bot` y `python -m dian_automation.cli.scheduler`, orquestados por `docker/entrypoint.sh`). Si la migración falla, `docker compose logs migrate` explica el motivo y los servicios de aplicación no inician.
   - **Aislamiento de base de datos:** el servicio `postgres` no publica puertos hacia el exterior en el host; la comunicación se realiza exclusivamente por la red interna de Docker.
4. Carga el calendario una sola vez: `docker compose exec api python -m dian_automation.core.calendar_loader data/calendario_dian_2026.csv`. Sin él, la API responde 409 en `/api/calendar` y el bot avisa que no está cargado (sección 8 para el año siguiente).
5. Deja `SCHEDULER_ENABLED=false` hasta que el worker remoto arranque solo.

`POSTGRES_PASSWORD` solo se lee cuando el volumen `postgres_data` se crea por primera vez: cambiarla después en el `.env` no cambia la contraseña de la base ya creada (habría que cambiarla dentro de PostgreSQL con `ALTER USER`, o recrear el volumen). Dentro de la red de compose, Redis es el host `redis` y PostgreSQL es el host `postgres`: `docker-compose.yml` fija sus conexiones para cada contenedor y no usa las del `.env`. El Docker no incluye worker ni Chrome: la descarga la hace siempre el worker remoto de las secciones 1 a 3.

### Copias de seguridad automáticas y retención

El servicio `backup` (`docker/pg-backup.sh`) genera una copia de seguridad cada 24 horas usando `pg_dump --format=custom`.
- Los respaldos se guardan en `./backups/` con una política de retención de 14 días (`BACKUP_KEEP_DAYS=14`).
- **Aviso importante:** Los respaldos se almacenan en el disco del mismo servidor. Se deben copiar o sincronizar periódicamente hacia una ubicación externa (almacenamiento en la nube o servidor secundario) para prevenir pérdidas por desastre.

### Procedimiento de restauración

Para restaurar una copia de seguridad:

1. Detén los servicios dependientes:
   ```bash
   docker compose stop api bot scheduler
   ```
2. Restaura el archivo deseado sobre la base de datos:
   ```bash
   docker compose exec -T postgres pg_restore -U kontable -d kontable --clean --if-exists --no-owner < backups/<archivo>.dump
   ```
   *(Alternativamente, si la base estuviera corrupta, créala vacía antes de restaurar).*
3. Inicia de nuevo los servicios:
   ```bash
   docker compose start api bot scheduler
   ```
4. **Validación:** Se debe ensayar la restauración en un entorno controlado al menos una vez para verificar la validez de las copias.

### Traslado de datos desde un SQLite existente

Para migrar la información de un `kontable.db` previo a PostgreSQL sin perder transaccionalidad:

1. Asegura que la base destino en PostgreSQL tenga el esquema en `head`:
   ```bash
   docker compose run --rm migrate
   ```
2. Asegura que la base SQLite origen esté en la revisión `head`:
   ```bash
   DATABASE_URL=sqlite:///./kontable.db uv run alembic upgrade head
   ```
3. Detén los servicios en producción:
   ```bash
   docker compose stop api bot scheduler
   ```
4. Ejecuta el script dentro del contenedor `migrate`, que ya trae `DATABASE_URL` hacia PostgreSQL (por eso no se pasa `--postgres`); el SQLite se monta de solo lectura:
   ```bash
   docker compose run --rm -v "$PWD/kontable.db:/data/kontable.db:ro" migrate python scripts/ops/migrate_sqlite_to_postgres.py --sqlite /data/kontable.db
   ```
   El script cancela sin tocar nada si el destino ya tiene datos o si las revisiones de esquema difieren; usa `--truncate` solo si el destino tiene datos de prueba que quieres sobrescribir. Imprime únicamente nombres de tabla y conteos. PostgreSQL no publica puertos, por eso el script corre dentro de la red de Docker y no desde el host.
5. Arranca los servicios apuntando a PostgreSQL:
   ```bash
   docker compose start api bot scheduler
   ```

### Corregir un ID de Telegram mal configurado

Si se configuró un ID erróneo en las variables de Telegram o un Chat ID quedó vinculado a una cuenta equivocada:

1. **Obtener el Chat ID real:** El usuario debe escribir `/mi_id` en Telegram para conocer su identificador numérico exacto.
2. **Actualizar variables en el VPS:** En el `.env` del VPS, ajusta `ADMIN_TELEGRAM_CHAT_ID` (para la administradora principal) y/o `ADMIN_BOOTSTRAP_CHAT_IDS` (lista de chats autorizados separada por comas). Ten presente que `/hacerme_admin` solo funciona para los chats listados en esas variables; si no se configura ninguna allowlist, el comando queda deshabilitado por seguridad por defecto.
3. **Recrear el contenedor del bot:** Aplica los cambios forzando la recreación del servicio para que tome las nuevas variables del archivo `.env`:
   ```bash
   docker compose up -d --force-recreate bot
   ```
   *(Nota: un simple `docker compose restart` no relee modificaciones en el archivo `.env`).*
4. **Desvincular un ID asociado a una cuenta equivocada:**
   - **Con una administradora vigente:** Ejecuta desde Telegram el comando:
     ```text
     /liberar_telegram <chat_id>
     ```
   - **Si no hay ningún administrador disponible:** Ejecuta la actualización directamente en la base de datos PostgreSQL:
     ```bash
     docker compose exec postgres psql -U kontable -d kontable -c "UPDATE users SET telegram_chat_id = NULL, is_telegram_linked = false WHERE telegram_chat_id = <chat_id>;"
     ```

