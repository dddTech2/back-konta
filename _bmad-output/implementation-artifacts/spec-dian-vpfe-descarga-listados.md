---
title: 'Automatización DIAN VPFE: Login con token de correo y encolamiento de reportes'
type: 'feature'
created: '2026-09-13'
status: 'done'
baseline_commit: 'NO_VCS'
route: 'full'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** La DIAN exige autenticación mediante enlace temporal enviado al correo registrado (RUT) sin contraseña directa, lo que impide descargar listados de facturación electrónica de forma desatendida.

**Approach:** Automatizar el flujo en Python (con `uv` y Playwright) consultando en segundo plano el buzón `token@example.com` en Stalwart Mail Server vía IMAP nativo para extraer el enlace de acceso, navegar al portal autenticado, posicionar el rango de fechas con `#action-left` / `#action-right` según el mes solicitado y solicitar la exportación a Excel hasta confirmar el encolamiento.

## Decisions

- **Runtime y Lenguaje:** Python 3 gestionado mediante `uv`, con `playwright` para automatización web e `imaplib` nativo de la biblioteca estándar para consulta de correo.
- **Gestión de Credenciales:** Esquema de configuración mediante variables de entorno en archivo `.env` (`DIAN_URL`, `DIAN_PERSON_CODE`, `STALWART_IMAP_HOST`, `STALWART_IMAP_PORT`, `STALWART_USER`, `STALWART_PASSWORD`) con plantilla `.env.example`.
- **Parámetro de fecha:** El script acepta el mes de descarga (ej. `--mes YYYY-MM`) como parámetro CLI; el bot compara el valor actual de `#export-range` y realiza los clics correspondientes en `#action-left` o `#action-right` hasta ubicar el rango en el mes especificado.

## Boundaries & Constraints

**Always:**
- Conectar a Stalwart directamente por protocolo IMAP SSL (puerto 993) en segundo plano; nunca abrir webmail en pestañas del navegador.
- Reusar el mismo `BrowserContext` de Playwright para que las cookies de sesión se preserven tras acceder al `tokenUrl`.
- Filtrar en el correo únicamente mensajes de la DIAN recibidos después del momento de clic en "Entrar" para evitar tokens caducados.
- Validar el valor actual de `#export-range` antes y después de cada clic en `#action-left`/`#action-right` con límite máximo de pasos de seguridad para prevenir bucles infinitos.
- Manejar credenciales de Stalwart y DIAN mediante variables de entorno en archivo `.env` (nunca en código fuente).

**Never:**
- No intentar descargar el archivo Excel en Milestone 1 si la DIAN lo encola en la tabla asíncrona; el Milestone 1 finaliza con la confirmación del modal y captura de la tabla.
- No automatizar interfaces web de clientes de correo.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Flujo exitoso | Cédula válida + correo con token recibido en <60s + `--mes 2026-08` | Navega a Histórico, ajusta fechas al mes indicado, confirma encolamiento y captura screenshot | Si la confirmación tarda, espera selectores explícitos |
| Correo retrasado | Clic en Entrar pero correo no llega a Stalwart en 60s | Reintentos cada 2s hasta agotar timeout | Lanza TimeoutError descriptivo sin colgar el proceso |
| Token inválido / expirado | Enlace abierto tras tiempo límite de DIAN | DIAN muestra pantalla de error de token | Detecta mensaje de error en URL o DOM y reporta fallo |
| Fechas ya alineadas | `#export-range` ya coincide con el mes solicitado | No realiza clics en `#action-left`/`#action-right` y procede a exportar | Validación previa |

</frozen-after-approval>

## Code Map

- `src/dian_automation/config.py` -- Carga de variables de entorno (`.env`), constantes de selectores DIAN y timeouts.
- `src/dian_automation/mail_client.py` -- Cliente IMAP con `imaplib` nativo para polling y regex parser del enlace de token desde el correo.
- `src/dian_automation/dian_flow.py` -- Flujo principal de Playwright: login, navegación, ajuste de `#export-range` por mes y confirmación de exportación.
- `src/dian_automation/__init__.py` -- Exportaciones del paquete modular.
- `tests/test_mail_parser.py` -- Pruebas unitarias de extracción de token, parser de fechas y timeout.
- `.env.example` / `.env` -- Plantilla y configuración local segura.
- `pyproject.toml` -- Definición de proyecto y dependencias gestionadas por `uv`.

## Tasks & Acceptance

**Execution:**
- [x] `pyproject.toml` -- Inicialización de proyecto Python con dependencias `playwright`, `python-dotenv`, `pytest` mediante `uv` -- Entorno ejecutable
- [x] `.env.example` -- Plantilla de variables de entorno para cédula DIAN y credenciales IMAP Stalwart -- Configuración segura
- [x] `src/dian_automation/config.py` -- Definición de selectores de DIAN y lectura de configuración -- Centralización de parámetros
- [x] `src/dian_automation/mail_client.py` -- Conexión IMAP a Stalwart, polling y regex parser de token -- Adquisición desatendida del token
- [x] `src/dian_automation/dian_flow.py` -- Script de automatización Playwright para flujo completo Milestone 1 -- Ejecución del flujo
- [x] `tests/test_mail_parser.py` -- Prueba unitaria de extracción de enlace sobre el HTML muestra del correo DIAN -- Garantía de parsing

**Acceptance Criteria:**
- Given la cédula `1000000001` y credenciales de correo configuradas, when se ejecuta el script, then la sesión inicia exitosamente en DIAN VPFE mediante el token capturado.
- Given la sesión autenticada en DIAN, when el script navega a "Descarga de listados", then valida y ajusta `#export-range` usando `#action-left`/`#action-right` según el mes indicado.
- Given el rango ajustado, when hace clic en "Exportar Excel" y confirma "SI", then cierra el modal con la "×" y genera una captura de pantalla `screenshot_encolado.png` mostrando la petición registrada en la tabla inferior.

## Implementation Notes

- Se inicializó el entorno virtual y dependencias con `uv` en Python 3.14 (`playwright`, `python-dotenv`, `pytest`).
- Se instalaron los navegadores de Playwright (`uv run playwright install chromium`).
- Se estructuró el código bajo el paquete modular `src/dian_automation` para compatibilidad total con empaquetado estándar `uv_build` y ejecución por CLI `uv run dian-automation`.
- Se desarrolló `StalwartMailClient` usando la biblioteca estándar `imaplib` con SSL (puerto 993) para polling asíncrono con control de fecha mínima para evitar tokens caducados.
- Se implementó `extract_token_url` con estrategia doble: análisis de etiquetas HTML (`<a>` con `<img>` "ingreso") y expresiones regulares de respaldo para URLs del portal DIAN.
- Se implementó `adjust_date_range` con soporte para formatos `DD/MM/YYYY`, `DD-MM-YYYY` y `YYYY-MM-DD` y protección de bucle infinito (máximo 40 desplazamientos).
- Se validaron 5 pruebas unitarias con `pytest` al 100% de aprobación.

## Spec Change Log

<!-- Populated by review loops -->

## Review Triage Log

- `low` | `patch` | Espera explícita de visibilidad (`wait_for(state="visible")`) agregada antes de hacer clic en el botón 'SI' de confirmación para evitar fallos por animación del modal.
- `low` | `patch` | Parámetro `timeout=30` agregado a la conexión socket SSL de `IMAP4_SSL` para prevenir bloqueos por cuelgues de red con Stalwart.
- `false` | `n/a` | Hipótesis de concurrencia en lectura de bandeja: la instancia lee secuencialmente con select('INBOX') y no modifica estados de correo destructivamente.


## Design Notes

- **Extracción de URL del token:** El correo de la DIAN contiene un tag `<a href="...">` envolviendo una imagen con texto alternativo o nombre "ingreso". El parser buscará la URL que apunte a `catalogo-vpfe-hab.dian.gov.co` con parámetro de token.
- **Ajuste de `#export-range`:** Se extraerá el texto actual del input, se parseará la fecha inicial/final y se calculará el diferencial contra el mes objetivo para decidir el número de clics en `#action-left` o `#action-right`.

## Verification

```bash
# Verificación de parsing de correo
uv run pytest tests/test_mail_parser.py

# Ejecución del flujo de automatización (modo headed o headless)
uv run python -m src.dian_automation --mes 2026-08
```
