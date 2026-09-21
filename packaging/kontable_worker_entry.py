"""Lanzador del .exe del worker remoto (PyInstaller necesita un script como punto de entrada)."""

from dian_automation.cli.worker_remote import run

if __name__ == "__main__":
    run()
