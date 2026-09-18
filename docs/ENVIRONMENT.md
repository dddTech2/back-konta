# Auditoría de Variables de Entorno

> Documento generado automáticamente por `codebase-documenter`.

Este catálogo consolida todas las variables de entorno consumidas en el código fuente, cruzadas con los archivos `.env.example` y `.env`.

## Resumen
- **Variables detectadas en código:** 15
- **Variables definidas en .env.example:** 15

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

**Base existente creada con `create_all` (p. ej. el volumen `kontable_data`).** Se adopta con `stamp`, nunca con `upgrade` (fallaría con "already exists"). Es un paso manual y no se ejecuta al arrancar:

1. Detén el bot y el worker y haz una copia de la base. En Docker (el servicio `api` monta `kontable_data` en `/data`): `docker compose run --rm -v "${PWD}:/backup" api cp /data/kontable.db /backup/kontable_copia.db`.
2. Compara el esquema de la copia con `models.py`, desde la raíz del proyecto con una ruta absoluta (en PowerShell: `$env:DATABASE_URL="sqlite:///C:/ruta/kontable_copia.db"`; en bash: `DATABASE_URL=sqlite:////ruta/kontable_copia.db uv run ...`): `uv run alembic stamp head` y luego `uv run alembic check` (debe decir que no hay operaciones nuevas; una diferencia solo de longitud de `VARCHAR`, como `dian_extraction_jobs.target_period` 7 vs 30 en bases antiguas, es inocua en SQLite, que no aplica longitudes; cualquier otra diferencia hay que revisarla antes de continuar).
3. Si la copia coincide, repite `alembic stamp head` sobre la base real (con la copia del paso 1 como respaldo). Solo agrega la tabla `alembic_version`; no modifica tablas ni filas.

Si un `upgrade head` sobre una base nueva falla a la mitad (SQLite no revierte el DDL), borra el archivo de la base y vuelve a ejecutarlo; nunca hagas `stamp head` sobre un esquema parcial. Ojo: `alembic downgrade base` elimina las 9 tablas con todos sus datos; no lo ejecutes sobre una base real.

Para cambios de esquema futuros: `uv run alembic revision --autogenerate -m "<mensaje>"`, revisar y limpiar la revisión, y `uv run alembic upgrade head` a mano. `tests/test_alembic_baseline.py` falla si `models.py` diverge de las revisiones.

---

## Recomendaciones de Seguridad
1. Nunca agregues claves de producción ni contraseñas a repositorios públicos.
2. Mantén siempre actualizado el archivo `.env.example` cuando agregues nuevas variables al código.
3. Emplea gestores de secretos (Vault, Doppler, AWS Secrets Manager) para despliegues de producción.
