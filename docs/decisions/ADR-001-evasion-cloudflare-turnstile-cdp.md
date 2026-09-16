# ADR-001: Evasión de Cloudflare Turnstile mediante Chrome Nativo y CDP

## Status
Accepted

## Date
2026-09-13

## Context
El portal de Facturación Electrónica de la DIAN (**VPFE**) protege sus endpoints de acceso (`/User/PersonLogin` y `/User/CompanyLogin`) con el mecanismo de protección anti-bot **Cloudflare Turnstile**.

Requisitos y restricciones del sistema:
- La automatización debe operar de forma **100% desatendida** (sin que un humano deba resolver captchas manualmente).
- No se deben pagar servicios externos de resolución de captcha (como 2Captcha o CapSolver) para evitar costos operativos recurrentes y dependencias externas lentas.
- Cloudflare Turnstile realiza análisis exhaustivo del entorno del navegador: inspecciona banderas de automatización (`navigator.webdriver`, `window.cdc_...`), huellas TLS/JA3/JA4, consistencia de WebGL y comportamiento del motor V8.
- Los navegadores Chromium sintéticos emulados por defecto en herramientas como Playwright (`playwright.chromium.launch()`) o Selenium son detectados de inmediato por las reglas heurísticas de Turnstile, quedando atrapados en bucles de validación infinita o bloqueos definitivos.

## Decision
Utilizar un proceso independiente de **Google Chrome nativo** instalado en el sistema operativo, lanzado con un perfil de usuario persistente (`.browser_profile`) y un puerto de depuración remota (`--remote-debugging-port=9222`), conectando Playwright a este proceso mediante el protocolo **CDP** (`chromium.connect_over_cdp()`).

Adicionalmente, se inyecta la librería `playwright-stealth` para asegurar que las propiedades del contexto de ejecución coincidan con las de un usuario humano regular.

## Alternatives Considered

### 1. Navegador Sintético de Playwright (`chromium.launch(headless=True)`)
- **Ventajas:** Cero dependencias del sistema operativo (Playwright descarga su propio binario de Chromium autónomo), fácil de empaquetar en contenedores mínimos.
- **Desventajas:** Cloudflare Turnstile detecta las diferencias en el binario y el flag `--enable-automation`. Incluso con `playwright-stealth`, las firmas de canvas/WebGL y las extensiones internas de Chromium de prueba delatan el script.
- **Rechazada:** Tasa de fallo del 100% ante los desafíos de Cloudflare en los portales gubernamentales de la DIAN.

### 2. Selenium con `undetected-chromedriver`
- **Ventajas:** Solución popular en Python para evadir detecciones básicas de Cloudflare.
- **Desventajas:** Altamente dependiente de la versión binaria exacta de Chrome instalada; falla frecuentemente tras actualizaciones automáticas de Chrome; está basado en llamadas síncronas bloqueantes incompatibles con la arquitectura asíncrona de Playwright y el sondeo concurrente de correo.
- **Rechazada:** Introduce inestabilidad de mantenimiento y bloquea el bucle de eventos `asyncio`.

### 3. Servicios Externos de Captcha Solving (2Captcha, CapSolver, Anti-Captcha)
- **Ventajas:** No requiere preocuparse por la evasión directa en el cliente.
- **Desventajas:** Introduce latencias de 15 a 45 segundos por intento; genera costos recurrentes por cada solicitud; requiere transmitir tokens y datos sensibles del portal tributario a servidores de terceros fuera de Colombia.
- **Rechazada:** Inviable por costos, latencia y cumplimiento de privacidad de datos tributarios.

## Consequences

### Positivas
- **Evasión Efectiva y Gratuita:** Cloudflare Turnstile se resuelve automáticamente y sin fricción en un rango de 3 a 5 segundos sin costo alguno.
- **Persistencia de Estado:** El uso de `--user-data-dir=.browser_profile` permite que el navegador conserve cookies de sesión válidas, almacenamiento local y caché, reduciendo la frecuencia con la que la DIAN exige verificaciones adicionales.
- **Arquitectura Asíncrona Limpia:** Playwright interactúa mediante WebSockets y CDP sin bloquear el hilo de ejecución principal de Python.

### Negativas / Mitigaciones
- **Dependencia de Chrome en el Host:** El entorno de ejecución (máquina local o contenedor) debe tener Google Chrome oficial instalado (`google-chrome-stable` en Linux o ejecutable estándar en Windows).
  * *Mitigación:* Se documenta el requisito y la ruta de detección en el [README.md](../../README.md) y se implementa fallback automático de rutas en `dian_flow.py`.
- **Gestión de Procesos Zombie:** Si el script se interrumpe de forma abrupta, el proceso de Chrome en background podría quedar escuchando en el puerto 9222.
  * *Mitigación:* El orquestador `dian_flow.py` implementa bloques `try/finally` y rutinas de limpieza para finalizar el proceso del navegador y liberar el puerto.
