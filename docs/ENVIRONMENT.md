# Auditoría de Variables de Entorno

> Documento generado automáticamente por `codebase-documenter`.

Este catálogo consolida todas las variables de entorno consumidas en el código fuente, cruzadas con los archivos `.env.example` y `.env`.

## Resumen
- **Variables detectadas en código:** 17
- **Variables definidas en .env.example:** 17

---

## Tabla de Variables

| Variable | Descripción / Rol | Valor por Defecto / Ejemplo | Archivos donde se consume | Estado |
| :--- | :--- | :--- | :--- | :--- |
| `DATABASE_URL` | URL SQLAlchemy de la base; la usan la app y Alembic (`alembic/env.py`) | `sqlite:///./kontable.db` (en Docker: `sqlite:////data/kontable.db`) | `src/dian_automation/db/database.py:7`, `alembic/env.py` | ✅ Documentada |
| `DIAN_COMPANY_NIT` |  | —, `901008579` (ejemplo) | `src/dian_automation/config.py:35` | ✅ Documentada |
| `DIAN_COMPANY_URL` |  | `https://catalogo-vpfe-hab.dian.gov.co/User/CompanyLogin`, `https://catalogo-vpfe-hab.dian.gov.co/User/CompanyLogin` (ejemplo) | `src/dian_automation/config.py:32` | ✅ Documentada |
| `DIAN_LOGIN_TYPE` | Modalidad de ingreso por defecto: 'persona' o 'empresa' | `persona`, `persona` (ejemplo) | `src/dian_automation/config.py:30` | ✅ Documentada |
| `DIAN_PERSON_CODE` | Modo Persona Natural: Cédula de Ciudadanía | `1000000001`, `1000000001` (ejemplo) | `src/dian_automation/config.py:33` | ✅ Documentada |
| `DIAN_REPRESENTATIVE_CODE` | Modo Empresa: Cédula del Representante Legal y NIT de la Empresa (sin DV) | —, `10000002` (ejemplo) | `src/dian_automation/config.py:34` | ✅ Documentada |
| `DIAN_URL` | Habilitación: | `https://catalogo-vpfe-hab.dian.gov.co/User/PersonLogin`, `https://catalogo-vpfe-hab.dian.gov.co/User/PersonLogin` (ejemplo) | `src/dian_automation/config.py:31` | ✅ Documentada |
| `DOWNLOAD_DIR` | Carpeta de destino de los archivos ZIP descargados | `./downloads`, `./downloads` (ejemplo) | `src/dian_automation/config.py:45` | ✅ Documentada |
| `EMAIL_TIMEOUT_SECONDS` | Tiempo máximo de espera para la llegada del correo con el token (segundos) | `60`, `60` (ejemplo) | `src/dian_automation/config.py:43` | ✅ Documentada |
| `EXPORT_DOWNLOAD_TIMEOUT_SECONDS` | Tiempo máximo de espera para que la DIAN procese y ponga en 'Listo' el ZIP (segundos) | `300`, `300` (ejemplo) | `src/dian_automation/config.py:44` | ✅ Documentada |
| `HEADLESS` | Ejecución visible o segundo plano (False recomendado para evitar bloqueos) | `False`, `False` (ejemplo) | `src/dian_automation/config.py:42` | ✅ Documentada |
| `JWT_SECRET` | Secreto que firma el JWT de sesión y el hash HMAC de los códigos OTP del login web; sin él no se puede iniciar sesión (la app arranca igual). Cambiarlo invalida sesiones y códigos pendientes | — (obligatorio para el login web) | `src/dian_automation/config.py`, `src/dian_automation/core/auth_service.py` | ✅ Documentada |
| `JWT_TTL_MINUTES` | Vigencia del JWT de sesión en minutos (sin refresh) | `60` | `src/dian_automation/config.py` | ✅ Documentada |
| `STALWART_IMAP_HOST` | Servidor de Correo Stalwart (Recepción automática del token mágico) | `mail.example.com`, `mail.example.com` (ejemplo) | `src/dian_automation/config.py:37` | ✅ Documentada |
| `STALWART_IMAP_PORT` |  | `993`, `993` (ejemplo) | `src/dian_automation/config.py:38` | ✅ Documentada |
| `STALWART_PASSWORD` |  | —, `tu_contrasena_aqui` (ejemplo) | `src/dian_automation/config.py:40` | ✅ Documentada |
| `STALWART_USER` |  | `token@example.com`, `token@example.com` (ejemplo) | `src/dian_automation/config.py:39` | ✅ Documentada |

---

## Base de datos y migraciones (Alembic)

Alembic es el dueño del esquema: ni el bot ni el worker crean tablas al arrancar (`init_db()` solo queda para scripts y desarrollo). `alembic/env.py` usa la misma `DATABASE_URL` que la aplicación (por defecto `sqlite:///./kontable.db`); `alembic.ini` no define URL.

**Despliegue nuevo (base vacía).** Antes de iniciar bot o worker:

```bash
uv run alembic upgrade head
# En Docker (usa DATABASE_URL=sqlite:////data/kontable.db del entrypoint):
docker compose run --rm api alembic upgrade head
```

**Base existente creada con `create_all` antes de Alembic (p. ej. el volumen `kontable_data`).** Esa base tiene el esquema de la revisión base `0001`, así que se adopta con `stamp 0001` (nunca `stamp head`, que marcaría como aplicadas revisiones posteriores cuyas tablas no existen, como `sales`) y luego `upgrade head` para aplicar las revisiones pendientes. Es un paso manual y no se ejecuta al arrancar:

1. Detén el bot y el worker y haz una copia de la base. En Docker (el servicio `api` monta `kontable_data` en `/data`): `docker compose run --rm -v "${PWD}:/backup" api cp /data/kontable.db /backup/kontable_copia.db`.
2. Ensaya sobre la copia, desde la raíz del proyecto y con una ruta absoluta (en PowerShell: `$env:DATABASE_URL="sqlite:///C:/ruta/kontable_copia.db"`; en bash: `DATABASE_URL=sqlite:////ruta/kontable_copia.db uv run ...`): `uv run alembic stamp 0001`, `uv run alembic upgrade head` y `uv run alembic check` (debe decir que no hay operaciones nuevas; una diferencia solo de longitud de `VARCHAR`, como `dian_extraction_jobs.target_period` 7 vs 30 en bases antiguas, es inocua en SQLite, que no aplica longitudes; cualquier otra diferencia hay que revisarla antes de continuar).
3. Si la copia coincide, repite `alembic stamp 0001` y `alembic upgrade head` sobre la base real (con la copia del paso 1 como respaldo). `stamp` solo agrega la tabla `alembic_version`; `upgrade head` crea únicamente las tablas nuevas.

Si la base ya contiene las tablas de una revisión posterior (por ejemplo `sales`, creada por un script que llama `init_db()`), `upgrade head` fallará con "already exists": estampa entonces la revisión que corresponda a su esquema real (`alembic stamp <revisión>`) tras compararlo con `models.py`.

Si un `upgrade head` sobre una base nueva falla a la mitad (SQLite no revierte el DDL), borra el archivo de la base y vuelve a ejecutarlo; nunca hagas `stamp` sobre un esquema parcial. Ojo: `alembic downgrade base` elimina todas las tablas con todos sus datos; no lo ejecutes sobre una base real.

Para cambios de esquema futuros: `uv run alembic revision --autogenerate -m "<mensaje>"`, revisar y limpiar la revisión, y `uv run alembic upgrade head` a mano. `tests/test_alembic_baseline.py` falla si `models.py` diverge de las revisiones.

---

## Autenticación web (OTP por Telegram + JWT)

El login web no usa contraseñas: `POST /api/auth/request-otp` recibe el teléfono o NIT del contribuyente (`identifier`) y envía un código de 6 dígitos a su Telegram vinculado (vigente 5 minutos, un solo uso, máximo 3 solicitudes por usuario cada 10 minutos y 5 intentos por código); `POST /api/auth/verify-otp` (`identifier` y `code`) lo canjea por un JWT Bearer de `JWT_TTL_MINUTES` minutos; `GET /api/auth/me` informa el negocio activo y el estado de la suscripción. Las rutas `/api/dashboard|iva|invoices/{negocio}` exigen ese JWT y responden 404 si el negocio no pertenece al usuario.

Requisitos de entorno: `JWT_SECRET` (obligatorio; usa al menos 32 caracteres aleatorios, p. ej. `python -c "import secrets; print(secrets.token_urlsafe(48))"`) y el token del bot de clientes (`TELEGRAM_BOT_TOKEN`, o los mismos alternativos que usa el bot) en el proceso de la API, porque es la API la que envía el código. Cambiar `JWT_SECRET` invalida todas las sesiones y los códigos pendientes. La tabla `otp_codes` se crea con la revisión Alembic `0003` (`alembic upgrade head`).

---

## Frontend web (SPA React + Vite)

La SPA vive en `../ProyectoDianFront` (React 18 + Vite 5 + TypeScript) y se compila a estático; en producción no hay Node, la API sirve `dist/` en `/app`.

```bash
cd ../ProyectoDianFront
npm ci && npm run build   # genera dist/ (no se commitea)
docker compose up -d      # FRONTEND_DIR=/frontend/dist ya está fijado en docker-compose.yml
```

- `FRONTEND_DIR`: carpeta que la API monta en `/app`. Fuera de Docker apunta a `../ProyectoDianFront/dist` (valor de `.env.example`); no lo dejes vacío, porque entonces la API sirve la carpeta del repo sin compilar (el `index.html` de Vite, que no funciona sin build). Si `dist/` no existe, la API arranca pero omite el mount `/app` sin avisar. El prototipo `kontable-prototipo_1.html` sigue en el repo como referencia, pero `dist/` no lo incluye y ya no obtiene datos (las rutas exigen JWT).
- `KONTABLE_WEB_URL`: URL pública de la SPA (`.../app/`) que el bot manda en `/dashboard`. La SPA ignora `?nit=`; el acceso es por OTP. Los servidores que ya tenían la URL del prototipo (`.../app/kontable-prototipo_1.html`) deben actualizar esta variable: el build no incluye ese archivo y el enlace antiguo daría 404.
- Desarrollo: `npm run dev` en `ProyectoDianFront` sirve la SPA en `http://localhost:5173/app/` y redirige `/api` a `http://127.0.0.1:8000` (o a `VITE_DEV_API`).
- Variables de la SPA (se fijan al compilar): `VITE_API_BASE` (por defecto vacío = mismo origen) y `VITE_KATERINN_WHATSAPP` (solo dígitos con indicativo; por defecto el número del prototipo, `573001234567`).
- El enrutado es por hash (`/app/#/login`), así que el servidor de estáticos no necesita fallback de SPA.

---

## Recomendaciones de Seguridad
1. Nunca agregues claves de producción ni contraseñas a repositorios públicos.
2. Mantén siempre actualizado el archivo `.env.example` cuando agregues nuevas variables al código.
3. Emplea gestores de secretos (Vault, Doppler, AWS Secrets Manager) para despliegues de producción.
