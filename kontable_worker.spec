# -*- mode: python ; coding: utf-8 -*-
"""Especificación de PyInstaller del worker remoto de Kontable (Windows).

Construir (desde la carpeta de ProyectoDianBack):

    uv run --with pyinstaller pyinstaller kontable_worker.spec --noconfirm

Resultado: dist/kontable-worker/kontable-worker.exe (carpeta, no un único archivo).

Decisiones:
  * Modo carpeta (onedir) y no un solo .exe: el modo de un archivo se descomprime en %TEMP% en cada
    arranque (lento al reiniciarse la tarea cada minuto) y los antivirus lo marcan con más frecuencia.
  * Consola visible: el worker escribe su registro por stdout y la tarea programada lo redirige a
    worker.log (ver scripts/install_worker_task.ps1).
  * No empaqueta un navegador: el flujo lanza el Chrome instalado (find_chrome_executable) y se
    conecta por CDP, así que solo hace falta el driver de Playwright, que trae el propio paquete.
  * No empaqueta el .env ni ningún dato: el .env se busca en la carpeta desde la que se lanza el
    programa (directorio de trabajo), igual que con `uv run`.
  * Solo el worker: el servidor (FastAPI, SQLAlchemy, Alembic, Uvicorn, bot de Telegram) se excluye.
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# El worker importa `dian_automation` (config, mail_client, dian_flow, queue.exceptions,
# queue.redis_signal) y usa httpx, playwright, python-dotenv, redis y tzdata. Se recolectan los
# submódulos de httpx y de tzdata porque se cargan de forma dinámica.
hiddenimports = (
    collect_submodules("dian_automation.queue")
    + collect_submodules("httpx")
    + ["redis", "dotenv"]
)

datas = collect_data_files("tzdata")  # zonas horarias (America/Bogota) en Windows, que no trae base de zonas

# Servidor y herramientas que el worker no usa
excludes = [
    "fastapi",
    "starlette",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "jwt",
    "multipart",
    "openpyxl",
    "holidays",
    "pytest",
    "tkinter",
    "dian_automation.api",
    "dian_automation.db",
    "dian_automation.core",
    "dian_automation.telegram",
    "dian_automation.subscriptions",
    "dian_automation.extraction",
]

a = Analysis(
    ["run_worker_remote.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="kontable-worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX aumenta las falsas alertas de antivirus
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="kontable-worker",
)
