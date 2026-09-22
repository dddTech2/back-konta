# -*- mode: python ; coding: utf-8 -*-
"""Especificación de PyInstaller del worker remoto de Konta (Windows) — un solo archivo.

Construir (desde la carpeta de ProyectoDianBack):

    uv run --with pyinstaller pyinstaller packaging/kontable_worker_onefile.spec --noconfirm

Resultado: dist/kontable-worker.exe (un ÚNICO archivo, sin carpeta _internal).

Cuándo usar este spec en vez de kontable_worker.spec (modo carpeta):
  * Para copiar el worker a otro computador con un solo archivo: no hay forma de olvidar _internal.
  * Contrapartidas del archivo único: se descomprime en %TEMP% en cada arranque (arranque más lento)
    y algunos antivirus lo marcan con más frecuencia. Si el worker se reinicia muy seguido, prefiere
    el modo carpeta.

Notas comunes con el modo carpeta:
  * El worker.log y el .env se buscan JUNTO al .exe (Path(sys.executable).parent), que en modo archivo
    apunta al .exe real, no a la carpeta temporal de descompresión. Así el registro y la configuración
    quedan al lado del ejecutable que copiaste.
  * No empaqueta un navegador: el flujo lanza el Chrome instalado y se conecta por CDP.
  * No empaqueta el .env ni ningún dato.
  * Solo el worker: el servidor (FastAPI, SQLAlchemy, Alembic, Uvicorn, bot de Telegram) se excluye.
"""
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = (
    collect_submodules("dian_automation.queue")
    + collect_submodules("httpx")
    + ["redis", "dotenv", "dian_automation.cli.worker_remote"]
)

datas = collect_data_files("tzdata")  # zonas horarias (America/Bogota) en Windows

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
    [os.path.join(SPECPATH, "kontable_worker_entry.py")],
    pathex=[os.path.join(SPECPATH, "..", "src")],
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

# Modo archivo único: todos los binarios y datos van DENTRO del .exe (sin COLLECT).
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="kontable-worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX aumenta las falsas alertas de antivirus
    console=True,
)
