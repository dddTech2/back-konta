# Despliegue del worker remoto

El servidor (API, bot, `scheduler`, Redis) corre en un VPS Linux cuya IP la DIAN bloquea. Las descargas las hace el **worker remoto** (`run_worker_remote.py`) desde una red residencial. Este documento cubre el equipo del worker; el servidor se levanta con `docker-compose.yml`.

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
uv run python run_worker_remote.py
```

Debe imprimir `Worker remoto 'remote' iniciado` y, sin trabajos, quedar esperando.

## 3. Arranque automático

### Windows (tarea programada)

Desde la carpeta del proyecto, en PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_worker_task.ps1 -StartNow
```

Registra la tarea **"Kontable Worker"**: arranca al iniciar sesión el usuario actual, se reinicia cada minuto si el worker termina con error y guarda su registro en `worker.log`. Para quitarla:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_worker_task.ps1 -Uninstall
```

Si el equipo es dedicado, activa el inicio de sesión automático (`netplwiz`) y desactiva la suspensión, para que el worker vuelva solo tras un corte de luz. No es un servicio de Windows a propósito: un servicio no tiene sesión de escritorio y Chrome no podría abrir ventana. Pasar a servicio exige `HEADLESS=True` y comprobar que Turnstile lo acepta.

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
ExecStart=/usr/bin/env uv run python run_worker_remote.py
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
