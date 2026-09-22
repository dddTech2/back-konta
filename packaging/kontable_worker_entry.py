"""Lanzador del .exe del worker remoto (PyInstaller necesita un script como punto de entrada).

Configura el registro a archivo ANTES de importar el worker, de modo que cualquier fallo
—incluso uno de importación o de arranque— quede escrito en worker.log junto al ejecutable.
Sin esto, al ejecutar el .exe con doble clic la ventana se cierra sin dejar rastro del error.
"""

import logging
import sys
import tempfile
from pathlib import Path


def _base_dir() -> Path:
    """Carpeta del .exe cuando está empaquetado; el directorio actual en desarrollo."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _setup_early_logging() -> Path:
    """Registra en worker.log (junto al .exe) y en consola. Devuelve la ruta real del archivo.

    Si no se puede escribir junto al .exe (p. ej. carpeta protegida), cae a la carpeta temporal.
    """
    log_path = _base_dir() / "worker.log"
    try:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
    except OSError:
        log_path = Path(tempfile.gettempdir()) / "kontable-worker.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")

    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    return log_path


if __name__ == "__main__":
    log_path = _setup_early_logging()
    log = logging.getLogger("worker_entry")
    log.info("Arrancando kontable-worker. El registro se guarda en: %s", log_path)
    try:
        from dian_automation.cli.worker_remote import run

        run()
    except Exception:
        # Cubre fallos de importación (dependencias faltantes en el equipo) y errores de arranque.
        log.exception("El worker terminó por un error no controlado durante el arranque o la ejecución.")
        if sys.stdin is not None and sys.stdin.isatty():
            try:
                input(f"\nOcurrió un error. Revisa el detalle en:\n  {log_path}\n\nPresiona Enter para cerrar...")
            except EOFError:
                pass
        sys.exit(1)
