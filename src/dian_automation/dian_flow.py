import asyncio
import argparse
import calendar
import logging
import os
import random
import re
import shutil
import subprocess
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple
from playwright.async_api import async_playwright, Page, BrowserContext

from .config import config
from .mail_client import StalwartMailClient
from .queue.exceptions import ExportTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("DianAutomation")

def sanitize_nit(value: Optional[str]) -> str:
    """
    Limpia y sanitiza un NIT o número de documento.
    Si viene con dígito de verificación (ej. 901.234.567-1 o 901234567-8),
    descarta el DV tras el guión y remueve cualquier caracter no numérico.
    """
    if not value:
        return ""
    val = value.strip()
    if "-" in val:
        parts = val.split("-")
        if len(parts) == 2 and len(parts[1].strip()) == 1:
            val = parts[0]
    return re.sub(r'\D', '', val)

def parse_date_range(range_text: str) -> Optional[Tuple[datetime, datetime]]:
    """
    Parsea una cadena de rango de fechas con diversos formatos posibles en portales colombianos:
    - 'DD/MM/YYYY - DD/MM/YYYY'
    - 'DD-MM-YYYY - DD-MM-YYYY'
    - 'YYYY-MM-DD - YYYY-MM-DD'
    """
    if not range_text:
        return None

    parts = re.split(r'\s+(?:-|a|al)\s+', range_text.strip())
    if len(parts) != 2:
        return None

    formats = [
        "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d"
    ]

    d1, d2 = None, None
    for fmt in formats:
        if d1 is None:
            try:
                d1 = datetime.strptime(parts[0].strip(), fmt)
            except ValueError:
                pass
        if d2 is None:
            try:
                d2 = datetime.strptime(parts[1].strip(), fmt)
            except ValueError:
                pass

    if d1 and d2:
        return d1, d2
    return None

async def human_type(page: Page, selector: str, text: str) -> None:
    """
    Escribe texto en un campo simulando la cadencia y movimientos de un humano:
    hover previo, clic, pequeña pausa y tipeo carácter por carácter con variaciones de tiempo.
    """
    element = page.locator(selector).first
    await element.scroll_into_view_if_needed()
    await page.wait_for_timeout(random.randint(300, 600))
    await element.hover()
    await page.wait_for_timeout(random.randint(300, 700))
    await element.click()
    await page.wait_for_timeout(random.randint(400, 800))

    # Limpiar contenido previo si existiera
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await page.wait_for_timeout(random.randint(200, 400))

    for char in text:
        delay = random.randint(90, 180)
        await page.keyboard.type(char, delay=delay)
        # Vacilación humana ocasional
        if random.random() < 0.15:
            await page.wait_for_timeout(random.randint(120, 280))

    await page.wait_for_timeout(random.randint(500, 900))

@dataclass
class TurnstileCheckResult:
    """Diagnóstico detallado de la espera de Cloudflare Turnstile.

    `solved` es la única señal confiable de éxito (Cloudflare escribe el token en el
    input oculto cuando el reto queda resuelto, sea automático o por clic manual).
    `widget_detected` permite distinguir DOS fallas muy distintas cuando `solved` es False:
    el widget nunca cargó en pantalla (problema de red/carga) vs. el widget mostró el
    recuadro interactivo y nadie lo marcó a tiempo (Cloudflare escaló el riesgo de la sesión).
    """
    solved: bool
    widget_detected: bool
    elapsed_seconds: float
    screenshot_path: Optional[str]

    def __bool__(self) -> bool:
        return self.solved


async def wait_for_turnstile_ready(page: Page, timeout_seconds: int = 40) -> TurnstileCheckResult:
    """
    Espera a que Cloudflare Turnstile resuelva su verificación y genere el token de respuesta.
    Evita clics sintéticos automatizados dentro del iframe de Cloudflare para no disparar
    el error de integridad 600010.
    Si el sistema requiere confirmación, permite que el usuario haga clic de forma limpia y
    confiable, recordándoselo periódicamente, y deja capturas de pantalla como evidencia de
    en qué estado se quedó (para poder revisar después si mostró el recuadro o no).
    """
    logger.info("Esperando resolución de Cloudflare Turnstile...")
    start = time.time()
    widget_detected = False
    widget_screenshot_path: Optional[str] = None
    last_reminder = 0.0

    while (time.time() - start) < timeout_seconds:
        elapsed = time.time() - start

        # 1. Verificar si el token ya fue generado en los inputs ocultos
        for name in ["cf-turnstile-response", "g-recaptcha-response"]:
            locator = page.locator(f'input[name="{name}"]')
            if await locator.count() > 0:
                val = await locator.first.input_value()
                if val and len(val) > 15:
                    logger.info(
                        f"Cloudflare Turnstile verificado exitosamente ({name} listo con token, {elapsed:.1f}s)."
                    )
                    return TurnstileCheckResult(
                        solved=True,
                        widget_detected=widget_detected,
                        elapsed_seconds=elapsed,
                        screenshot_path=widget_screenshot_path,
                    )

        # 2. Detectar el widget en pantalla, dejar evidencia y recordar periódicamente
        if not widget_detected:
            for frame in page.frames:
                if "turnstile" in frame.url or "challenges.cloudflare.com" in frame.url:
                    widget_detected = True
                    try:
                        widget_screenshot_path = "screenshot_turnstile_widget.png"
                        await page.screenshot(path=widget_screenshot_path, full_page=True)
                    except Exception:
                        widget_screenshot_path = None
                    logger.info(
                        "Widget de Turnstile detectado en pantalla "
                        f"(evidencia: {widget_screenshot_path}). Si muestra el recuadro "
                        "'Verifique que es un ser humano', haz clic en él con tu mouse."
                    )
                    last_reminder = elapsed
                    break
        elif (elapsed - last_reminder) > 10:
            logger.info(
                f"Turnstile sigue sin resolverse ({elapsed:.0f}s de {timeout_seconds}s) — "
                "revisa si el recuadro sigue en pantalla y haz clic."
            )
            last_reminder = elapsed

        await page.wait_for_timeout(600)

    elapsed = time.time() - start
    timeout_screenshot_path: Optional[str] = None
    try:
        timeout_screenshot_path = "screenshot_turnstile_timeout.png"
        await page.screenshot(path=timeout_screenshot_path, full_page=True)
    except Exception:
        timeout_screenshot_path = None

    logger.warning(
        f"Se cumplió el tiempo de espera para el token de Turnstile ({elapsed:.0f}s). "
        f"Widget detectado: {widget_detected}. Evidencia: {timeout_screenshot_path}."
    )
    return TurnstileCheckResult(
        solved=False,
        widget_detected=widget_detected,
        elapsed_seconds=elapsed,
        screenshot_path=timeout_screenshot_path,
    )

async def get_current_range_text(page: Page) -> str:
    """Obtiene el texto del campo de rango de fechas ya sea por input_value o inner_text."""
    locator = page.locator(config.selectors.EXPORT_RANGE)
    val = await locator.input_value()
    if not val:
        val = await locator.inner_text()
    return (val or "").strip()

def resolve_target_date_range(target: Optional[str]) -> Tuple[str, str]:
    """
    Resuelve el rango de fechas objetivo.
    Soporta:
    - Formato mes: 'YYYY-MM' (ej. '2026-08') -> ('2026-08-01', '2026-08-31')
    - Formato rango explícito: 'YYYY-MM-DD - YYYY-MM-DD'
    - None -> Mes anterior completo por defecto (ej. si hoy es sep-2026, toma ago-2026)
    """
    if not target:
        now = datetime.now()
        year = now.year
        month = now.month - 1
        if month == 0:
            month = 12
            year -= 1
        last_day = calendar.monthrange(year, month)[1]
        return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"

    target = target.strip()
    if "-" in target and len(target.split("-")) >= 5:
        parts = [p.strip() for p in re.split(r'\s+(?:-|a|al)\s+', target)]
        if len(parts) == 2:
            return parts[0], parts[1]

    dt = datetime.strptime(target, "%Y-%m")
    last_day = calendar.monthrange(dt.year, dt.month)[1]
    return f"{dt.year:04d}-{dt.month:02d}-01", f"{dt.year:04d}-{dt.month:02d}-{last_day:02d}"

async def set_date_range(page: Page, target: Optional[str] = None) -> Tuple[str, str]:
    """
    Configura el rango de fechas directamente 'por debajo' tanto en Bootstrap DateRangePicker
    como en los campos ocultos del formulario DIAN (#StartDate y #EndDate) que se serializan
    en el envío POST de /Document/Export.
    """
    start_date, end_date = resolve_target_date_range(target)
    logger.info(f"Configurando rango de fechas por debajo: {start_date} a {end_date}")

    result = await page.evaluate("""([start, end]) => {
        try {
            // 1. Asignar directamente los inputs del formulario (#StartDate y #EndDate)
            const startInput = document.querySelector('#StartDate');
            const endInput = document.querySelector('#EndDate');
            if (startInput) {
                startInput.value = start;
                startInput.dispatchEvent(new Event('input', { bubbles: true }));
                startInput.dispatchEvent(new Event('change', { bubbles: true }));
            }
            if (endInput) {
                endInput.value = end;
                endInput.dispatchEvent(new Event('input', { bubbles: true }));
                endInput.dispatchEvent(new Event('change', { bubbles: true }));
            }

            // 2. Si jQuery y DateRangePicker están presentes, sincronizar el widget
            if (window.$) {
                if ($('#StartDate').length) $('#StartDate').val(start).trigger('change');
                if ($('#EndDate').length) $('#EndDate').val(end).trigger('change');

                if (window.moment) {
                    if (typeof window.startDate !== 'undefined') window.startDate = moment(start);
                    if (typeof window.endDate !== 'undefined') window.endDate = moment(end);
                }

                if ($('#export-range').data('daterangepicker')) {
                    const picker = $('#export-range').data('daterangepicker');
                    const mStart = window.moment ? moment(start) : start;
                    const mEnd = window.moment ? moment(end) : end;
                    if (typeof picker.setStartDate === 'function') {
                        picker.setStartDate(mStart);
                        picker.setEndDate(mEnd);
                    }
                }
                $('#export-range').val(start + ' - ' + end).trigger('change');
            }

            return {
                success: true,
                startDateVal: startInput ? startInput.value : null,
                endDateVal: endInput ? endInput.value : null,
                rangeVal: document.querySelector('#export-range') ? document.querySelector('#export-range').value : null
            };
        } catch (e) {
            return { success: false, error: e.toString() };
        }
    }""", [start_date, end_date])

    logger.info(f"Resultado de configuración de rango de fechas: {result}")
    await page.wait_for_timeout(1000)
    return start_date, end_date

async def wait_and_download_export(
    page: Page,
    start_fmt: str,
    end_fmt: str,
    timeout_seconds: int = 300,
    download_dir: str = "./downloads"
) -> str:
    """
    Monitorea la tabla #tableExport hasta que la fila correspondiente al rango
    (Desde {start_fmt} Hasta {end_fmt}) pase a estado 'Listo' y descarga el archivo ZIP resultante.
    """
    logger.info(
        f"Iniciando monitoreo de descarga en tabla #tableExport para rango "
        f"Desde {start_fmt} Hasta {end_fmt} (timeout: {timeout_seconds}s)..."
    )
    os.makedirs(download_dir, exist_ok=True)
    try:
        await page.screenshot(path="screenshot_tabla_reportes.png", full_page=True)
        _art_dir = os.getenv("ARTIFACTS_DIR", "")
        if _art_dir and os.path.isdir(_art_dir):
            shutil.copy2("screenshot_tabla_reportes.png", os.path.join(_art_dir, "screenshot_tabla_reportes.png"))
    except Exception:
        pass
    start_time = time.time()
    poll_interval = 8

    while (time.time() - start_time) < timeout_seconds:
        elapsed = int(time.time() - start_time)
        rows = page.locator(config.selectors.TABLE_EXPORT_ROWS)
        row_count = await rows.count()

        if row_count == 0:
            rows = page.locator("table tbody tr")
            row_count = await rows.count()

        found_target_row = False
        for i in range(row_count):
            row = rows.nth(i)
            row_text = await row.inner_text()
            if start_fmt in row_text and end_fmt in row_text:
                found_target_row = True
                download_link = row.locator(config.selectors.DOWNLOAD_LINK).first
                ready_icon = row.locator(config.selectors.STATUS_READY_ICON).first
                
                is_ready = False
                if await download_link.count() > 0 and await download_link.is_visible():
                    is_ready = True
                elif await ready_icon.count() > 0 and await ready_icon.is_visible():
                    is_ready = True
                
                if is_ready:
                    logger.info("¡Reporte listo en tabla! Procediendo a descargar archivo ZIP...")
                    async with page.expect_download(timeout=60000) as download_info:
                        await download_link.scroll_into_view_if_needed()
                        await download_link.hover()
                        await page.wait_for_timeout(300)
                        await download_link.click()

                    download = await download_info.value
                    suggested = download.suggested_filename
                    if not suggested or not suggested.endswith(".zip"):
                        suggested = f"export_{start_fmt}_{end_fmt}.zip"

                    target_file = os.path.abspath(os.path.join(download_dir, suggested))
                    await download.save_as(target_file)

                    file_size = os.path.getsize(target_file) if os.path.exists(target_file) else 0
                    logger.info(f"Descarga exitosa: {target_file} ({file_size:,} bytes)")
                    
                    await page.wait_for_timeout(1000)
                    await page.screenshot(path="screenshot_descarga_completada.png", full_page=True)
                    # Copiar a artifacts si existe la variable de entorno
                    _art_dir = os.getenv("ARTIFACTS_DIR", "")
                    if _art_dir and os.path.isdir(_art_dir):
                        try:
                            shutil.copy2("screenshot_descarga_completada.png", os.path.join(_art_dir, "screenshot_descarga_completada.png"))
                        except Exception:
                            pass
                    return target_file
                else:
                    logger.info(
                        f"Reporte encontrado en cola (estado: en proceso / ⟳). "
                        f"Tiempo transcurrido: {elapsed}s / {timeout_seconds}s..."
                    )
                break

        if not found_target_row:
            logger.info(
                f"Buscando tarea en tabla ({row_count} filas visibles)... "
                f"Tiempo transcurrido: {elapsed}s / {timeout_seconds}s"
            )

        # Capturar pantallazo del estado de espera actual
        try:
            await page.screenshot(path="screenshot_espera.png", full_page=True)
            _art_dir = os.getenv("ARTIFACTS_DIR", "")
            if _art_dir and os.path.isdir(_art_dir):
                try:
                    shutil.copy2("screenshot_espera.png", os.path.join(_art_dir, "screenshot_espera.png"))
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"No se pudo guardar screenshot_espera.png: {e}")

        await page.wait_for_timeout(poll_interval * 1000)

        # Recargar la página para que el servidor DIAN devuelva la tabla con el estado actualizado
        try:
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
        except Exception as e:
            logger.debug(f"Error al recargar página durante sondeo: {e}")

    # Si se agota el tiempo, guardar evidencia del timeout
    try:
        await page.screenshot(path="screenshot_timeout_espera.png", full_page=True)
        _art_dir = os.getenv("ARTIFACTS_DIR", "")
        if _art_dir and os.path.isdir(_art_dir):
            try:
                shutil.copy2("screenshot_timeout_espera.png", os.path.join(_art_dir, "screenshot_timeout_espera.png"))
            except Exception:
                pass
    except Exception:
        pass

    raise ExportTimeoutError(
        f"Se superó el tiempo máximo de espera ({timeout_seconds}s) sin que el reporte para "
        f"Desde {start_fmt} Hasta {end_fmt} estuviera listo para descargar."
    )

def find_chrome_executable() -> str:
    """
    Busca el binario ejecutable de Google Chrome o Chromium tanto en Windows como en Linux/macOS.
    Permite sobrescribir la ruta mediante la variable de entorno CHROME_PATH o CHROME_BIN.
    """
    env_chrome = os.getenv("CHROME_PATH") or os.getenv("CHROME_BIN")
    if env_chrome and os.path.exists(env_chrome):
        return env_chrome

    # 1. Búsqueda en PATH del sistema (funciona tanto en Linux como en Windows)
    candidate_names = [
        "google-chrome-stable",
        "google-chrome",
        "chromium-browser",
        "chromium",
        "chrome",
        "chrome.exe",
    ]
    for name in candidate_names:
        found = shutil.which(name)
        if found:
            return found

    # 2. Rutas conocidas en Windows
    windows_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p_path in windows_paths:
        if os.path.exists(p_path):
            return p_path

    # 3. Rutas conocidas en Linux / Unix
    linux_paths = [
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
        "/usr/local/bin/chrome",
    ]
    for p_path in linux_paths:
        if os.path.exists(p_path):
            return p_path

    return "google-chrome" if os.name != "nt" else "chrome"


@asynccontextmanager
async def launch_chrome_and_connect(initial_url: str, port: int = 9222):
    """Lanza Chrome nativo con perfil persistente (.browser_profile) y conecta Playwright
    vía CDP (ver ADR-001: evasión de Cloudflare Turnstile). Reutilizable por `run_flow` y
    por herramientas de diagnóstico (ej. scripts/dev/check_turnstile.py) que necesitan la misma
    sesión de navegador sin repetir la lógica de lanzamiento.

    Yields (proc, browser, context, page). Cierra el browser y termina el proceso de Chrome
    al salir del bloque `async with`, incluso si ocurre una excepción dentro de él.
    """
    chrome_path = find_chrome_executable()
    logger.info(f"Binario de Chrome detectado: {chrome_path}")

    user_data_dir = os.path.abspath("./.browser_profile")
    os.makedirs(user_data_dir, exist_ok=True)
    logger.info(f"Usando perfil persistente en: {user_data_dir}")

    cmd = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
        initial_url,
    ]
    if os.name != "nt":
        cmd.extend([
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ])
    if config.headless:
        cmd.append("--headless=new")

    logger.info(f"Lanzando Google Chrome nativo sin banderas de automatización en puerto {port}...")
    proc = subprocess.Popen(cmd)
    await asyncio.sleep(2)

    async with async_playwright() as p:
        logger.info("Conectando Playwright vía CDP al navegador...")
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            yield proc, browser, context, page
        finally:
            try:
                await browser.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:
                pass


async def run_flow(
    target_month: Optional[str] = None,
    token_url: Optional[str] = None,
    login_type: Optional[str] = None,
    representative_code: Optional[str] = None,
    company_nit: Optional[str] = None,
    person_code: Optional[str] = None,
    download_dir: Optional[str] = None,
    timeout_seconds: Optional[int] = None
) -> str:
    """
    Ejecuta el flujo completo de automatización DIAN VPFE.
    Soporta modalidades 'persona' y 'empresa' (Representante legal).
    Si se suministra token_url, salta el login y accede directamente al portal autenticado.
    """
    # 1. Determinar modalidad y credenciales
    if not login_type:
        if company_nit:
            login_type = "empresa"
        else:
            login_type = config.login_type or "persona"
    login_type = login_type.lower().strip()

    if login_type == "empresa":
        rep_code = sanitize_nit(representative_code if representative_code is not None else config.dian_representative_code)
        comp_nit = sanitize_nit(company_nit if company_nit is not None else config.dian_company_nit)
        login_url = config.dian_company_url
        if not rep_code or not comp_nit:
            raise ValueError(
                "Para la modalidad 'empresa' se requiere la cédula del representante legal "
                "y el NIT de la empresa (sin DV)."
            )
        logger.info(f"Iniciando automatización DIAN VPFE [Modo: EMPRESA - NIT: {comp_nit}, Rep: {rep_code}]...")
    else:
        login_type = "persona"
        pers_code = sanitize_nit(person_code if person_code is not None else config.dian_person_code)
        login_url = config.dian_url
        if not pers_code:
            raise ValueError("Para la modalidad 'persona' se requiere la cédula del contribuyente.")
        logger.info(f"Iniciando automatización DIAN VPFE [Modo: PERSONA - Cédula: {pers_code}]...")

    screenshot_path = "screenshot_encolado.png"
    initial_url = token_url if token_url else login_url

    async with launch_chrome_and_connect(initial_url) as (proc, browser, context, page):
        if not token_url:
            # 1. Esperar carga inicial del login
            logger.info(f"Verificando carga de login ({login_url})...")
            if login_url not in page.url:
                await page.goto(login_url, wait_until="domcontentloaded")
            else:
                await page.wait_for_load_state("domcontentloaded")

            # Pausa humana tras cargar la página para permitir que Cloudflare Turnstile inicialice
            wait_load = random.uniform(2.5, 4.0)
            logger.info(f"Pausa natural de lectura ({wait_load:.1f}s)...")
            await page.wait_for_timeout(int(wait_load * 1000))

            if login_type == "empresa":
                # Clic en pestaña 'Representante legal'
                logger.info("Haciendo clic en la pestaña 'Representante legal'...")
                rep_tab = page.locator(config.selectors.REPRESENTATIVE_TAB_BUTTON).first
                await rep_tab.wait_for(state="visible", timeout=15000)
                await rep_tab.hover()
                await page.wait_for_timeout(random.randint(300, 600))
                await rep_tab.click()
                await page.wait_for_timeout(random.randint(600, 1000))

                # Tipear cédula del representante legal en #UserCode:visible
                logger.info(f"Tipeando cédula del representante legal: {rep_code}")
                rep_code_input = page.locator(config.selectors.REPRESENTATIVE_CODE).first
                await rep_code_input.wait_for(state="visible", timeout=15000)
                await human_type(page, config.selectors.REPRESENTATIVE_CODE, rep_code)

                # Tipear NIT de la empresa en #CompanyCode:visible
                logger.info(f"Tipeando NIT de la empresa (sin DV): {comp_nit}")
                comp_code_input = page.locator(config.selectors.COMPANY_CODE).first
                await comp_code_input.wait_for(state="visible", timeout=15000)
                await human_type(page, config.selectors.COMPANY_CODE, comp_nit)
            else:
                # Tipear cédula de persona natural
                logger.info(f"Tipeando cédula de persona natural: {pers_code}")
                person_code_input = page.locator(config.selectors.PERSON_CODE).first
                await person_code_input.wait_for(state="visible", timeout=15000)
                await human_type(page, config.selectors.PERSON_CODE, pers_code)

            # 3. Esperar a que Cloudflare Turnstile termine de verificar antes de hacer clic
            turnstile_result = await wait_for_turnstile_ready(page, timeout_seconds=60)
            if not turnstile_result.solved:
                if turnstile_result.widget_detected:
                    raise RuntimeError(
                        "Cloudflare Turnstile mostró el recuadro de verificación y no se marcó a "
                        f"tiempo ({turnstile_result.elapsed_seconds:.0f}s). Esto no es un problema de "
                        "configuración: Cloudflare decidió escalar el riesgo de la sesión (IP, "
                        "frecuencia de intentos recientes). Revisa "
                        f"{turnstile_result.screenshot_path} y, si corres con HEADLESS=False, haz clic "
                        "manualmente la próxima vez antes de que expire el tiempo."
                    )
                raise RuntimeError(
                    "Cloudflare Turnstile no completó la verificación y el widget nunca se detectó "
                    f"en pantalla ({turnstile_result.elapsed_seconds:.0f}s). Revisa "
                    f"{turnstile_result.screenshot_path} — probablemente sea un problema de red o de "
                    "carga de la página de login, no de Turnstile en sí."
                )

            # Pausa humana antes de enviar
            await page.wait_for_timeout(random.randint(800, 1600))

            # 4. Mover el ratón hacia el botón 'Entrar' y hacer clic
            enter_btn = page.locator(config.selectors.LOGIN_BUTTON).first
            await enter_btn.scroll_into_view_if_needed()
            await enter_btn.hover()
            await page.wait_for_timeout(random.randint(400, 800))

            t_click = datetime.now(timezone.utc)
            logger.info("Haciendo clic en 'Entrar'...")
            await enter_btn.click()

            # 5. Esperar y extraer token de correo vía Stalwart IMAP
            logger.info(f"Esperando correo con token en Stalwart ({config.stalwart_user})...")
            mail_client = StalwartMailClient(
                host=config.stalwart_host,
                port=config.stalwart_port,
                user=config.stalwart_user,
                password=config.stalwart_password
            )
                
            token_url = mail_client.wait_for_token_email(
                min_timestamp=t_click,
                timeout_seconds=config.email_timeout_seconds
            )
            mail_client.disconnect()
            logger.info(f"Token obtenido exitosamente: {token_url}")

            # 6. Navegar al enlace del token en el mismo contexto
            logger.info("Abriendo enlace mágico de autenticación en la sesión...")
            await page.goto(token_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
        else:
            logger.info(f"Usando enlace de token existente provisto: {token_url}")
            await page.goto(token_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)

        # 7. Asegurar que estamos en Descarga de listados (/Document/Export)
        if "/Document/Export" not in page.url:
            logger.info("Navegando a 'Descarga de listados'...")
            parsed = urllib.parse.urlparse(page.url)
            base_domain = parsed.netloc or "catalogo-vpfe.dian.gov.co"
            export_url = f"https://{base_domain}/Document/Export"
            await page.goto(export_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
        else:
            logger.info("La sesión ya se encuentra en 'Descarga de listados'.")

        await page.locator(config.selectors.EXPORT_RANGE).first.wait_for(state="visible", timeout=15000)

        start_date, end_date = resolve_target_date_range(target_month)
        start_fmt = datetime.strptime(start_date, "%Y-%m-%d").strftime("%d-%m-%Y")
        end_fmt = datetime.strptime(end_date, "%Y-%m-%d").strftime("%d-%m-%Y")

        # 8. Verificar si ya existe una tarea generada en la tabla para este rango
        existing_task = False
        rows = page.locator(config.selectors.TABLE_EXPORT_ROWS)
        row_count = await rows.count()
        if row_count == 0:
            rows = page.locator("table tbody tr")
            row_count = await rows.count()

        for i in range(row_count):
            row_text = await rows.nth(i).inner_text()
            if start_fmt in row_text and end_fmt in row_text:
                logger.info(f"Tarea encontrada en tabla para el rango Desde {start_fmt} Hasta {end_fmt}.")
                existing_task = True
                break

        # 9. Si no existe la tarea en cola o lista, solicitar la exportación
        if not existing_task:
            logger.info(f"No existe tarea previa para este rango. Solicitando nueva exportación ({start_fmt} al {end_fmt})...")
            start_used, end_used = await set_date_range(page, target_month)
            await page.screenshot(path="screenshot_antes_exportar.png", full_page=True)

            export_btn = page.locator(config.selectors.EXPORT_EXCEL_BUTTON).first
            await export_btn.scroll_into_view_if_needed()
            await export_btn.hover()
            await page.wait_for_timeout(random.randint(400, 700))
            await export_btn.click()

            si_btn = page.locator(config.selectors.CONFIRM_SI_BUTTON).first
            await si_btn.wait_for(state="visible", timeout=15000)
            await si_btn.hover()
            await page.wait_for_timeout(random.randint(300, 600))
            await si_btn.click()

            logger.info("Verificando diálogo de confirmación...")
            await page.wait_for_timeout(2000)
            close_btn = page.locator(config.selectors.CLOSE_MODAL_SPAN).first
            if await close_btn.count() > 0 and await close_btn.is_visible():
                await close_btn.hover()
                await close_btn.click()
                await page.wait_for_timeout(1000)

            await page.wait_for_timeout(5000)
            await page.screenshot(path=screenshot_path, full_page=True)
            logger.info(f"Evidencia de encolamiento guardada en: {screenshot_path}")

        # 10. Monitoreo de estado 'Listo' y descarga del archivo ZIP exportado
        out_dir = download_dir or config.download_dir
        t_wait = timeout_seconds or config.export_download_timeout_seconds

        downloaded_zip = await wait_and_download_export(
            page=page,
            start_fmt=start_fmt,
            end_fmt=end_fmt,
            timeout_seconds=t_wait,
            download_dir=out_dir
        )
        logger.info(f"Flujo completado con éxito. Archivo ZIP disponible en: {downloaded_zip}")
        return downloaded_zip


def main():
    parser = argparse.ArgumentParser(description="Automatización DIAN VPFE - Descarga de listados")
    parser.add_argument("--tipo", choices=["persona", "empresa"], default=None, help="Tipo de ingreso: 'persona' o 'empresa'")
    parser.add_argument("--nit-representante", type=str, default=None, help="Cédula/Documento del representante legal (modo empresa)")
    parser.add_argument("--nit-empresa", type=str, default=None, help="NIT de la empresa sin dígito de verificación (modo empresa)")
    parser.add_argument("--cedula", type=str, default=None, help="Cédula de persona natural (modo persona)")
    parser.add_argument("--mes", type=str, default=None, help="Mes objetivo en formato YYYY-MM (ej. 2026-08) o rango 'YYYY-MM-DD - YYYY-MM-DD'")
    parser.add_argument("--token-url", type=str, default=None, help="Enlace de token directo para saltar el login")
    parser.add_argument("--output-dir", type=str, default=None, help="Directorio de destino para el archivo ZIP descargado (default: ./downloads)")
    parser.add_argument("--download-timeout", type=int, default=None, help="Tiempo máximo de espera en segundos para la descarga (default: 300)")
    parser.add_argument("--headless", action="store_true", default=None, help="Ejecutar en modo sin cabeza (headless)")
    args = parser.parse_args()

    if args.headless is not None:
        object.__setattr__(config, "headless", args.headless)

    asyncio.run(
        run_flow(
            target_month=args.mes,
            token_url=args.token_url,
            login_type=args.tipo,
            representative_code=args.nit_representante,
            company_nit=args.nit_empresa,
            person_code=args.cedula,
            download_dir=args.output_dir,
            timeout_seconds=args.download_timeout
        )
    )

if __name__ == "__main__":
    main()

