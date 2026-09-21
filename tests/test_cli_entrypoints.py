"""Pruebas de verificación de los puntos de entrada CLI y estructura del proyecto (Tarea 1 de 2).

Valida que:
1. Los módulos en `src/dian_automation/cli/` importen limpiamente y expongan sus callables.
2. `pyproject.toml` defina los 5 comandos bajo `[project.scripts]` con sus destinos exactos.
3. No existan scripts `run_*.py` ni capturas `*.png` sueltas en la raíz del proyecto.
4. Existan los artefactos de empaquetado en `packaging/`.
"""

from pathlib import Path
import tomllib

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_cli_modules_import_and_expose_expected_callables():
    """Comprueba que los módulos CLI se importen correctamente y expongan las funciones esperadas."""
    from dian_automation import main as root_main
    from dian_automation.cli import scheduler, telegram_bot, worker, worker_remote

    assert callable(root_main)
    assert callable(getattr(worker_remote, "run", None))
    assert callable(getattr(worker_remote, "main", None))
    assert callable(getattr(worker, "main", None))
    assert callable(getattr(scheduler, "main", None))
    assert callable(getattr(telegram_bot, "main", None))


def test_pyproject_scripts_declaration():
    """Lee pyproject.toml con tomllib y verifica los 5 comandos de [project.scripts]."""
    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    assert pyproject_path.exists(), "pyproject.toml debe existir en la raíz"

    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    scripts = data.get("project", {}).get("scripts", {})

    expected_scripts = {
        "dian-automation": "dian_automation:main",
        "kontable-worker-remote": "dian_automation.cli.worker_remote:run",
        "kontable-worker": "dian_automation.cli.worker:main",
        "kontable-scheduler": "dian_automation.cli.scheduler:main",
        "kontable-bot": "dian_automation.cli.telegram_bot:main",
    }

    assert scripts == expected_scripts


def test_no_orphan_runners_or_loose_png_in_project_root():
    """Verifica que en la raíz NO queden scripts run_*.py ni imágenes *.png sueltas."""
    run_files = list(PROJECT_ROOT.glob("run_*.py"))
    assert run_files == [], f"No deben existir scripts run_*.py en la raíz: {run_files}"

    png_files = list(PROJECT_ROOT.glob("*.png"))
    assert png_files == [], f"No deben existir imágenes *.png en la raíz: {png_files}"


def test_packaging_artifacts_exist():
    """Verifica que los artefactos de empaquetado existan en packaging/."""
    spec_file = PROJECT_ROOT / "packaging" / "kontable_worker.spec"
    entry_file = PROJECT_ROOT / "packaging" / "kontable_worker_entry.py"

    assert spec_file.is_file(), f"Falta {spec_file}"
    assert entry_file.is_file(), f"Falta {entry_file}"

    # Validar que kontable_worker_entry.py invoque run()
    entry_content = entry_file.read_text(encoding="utf-8")
    assert "dian_automation.cli.worker_remote" in entry_content
    assert "run()" in entry_content
