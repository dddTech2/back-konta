"""Prueba de regresión para asegurar que no existan fechas límite hardcodeadas (AC #5)."""

from pathlib import Path
import pytest

FORBIDDEN_FRAGMENTS = [
    "10 prox. mes",
    "10 sept 2026",
    "Jul – Ago 2026",
    "dias=5",
    'mes="Ago"',
    'period_year_month="2026-08"',
    'periodo="2026-08"',
    'period_key="2026-08"',
]

# Rutas relativas al archivo de prueba con pathlib (no al cwd)
TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
ROUTES_IVA = REPO_ROOT / "src" / "dian_automation" / "api" / "routes_iva.py"
ROUTES_DASHBOARD = REPO_ROOT / "src" / "dian_automation" / "api" / "routes_dashboard.py"


@pytest.mark.parametrize("file_path", [ROUTES_IVA, ROUTES_DASHBOARD])
@pytest.mark.parametrize("fragment", FORBIDDEN_FRAGMENTS)
def test_no_hardcoded_deadlines(file_path: Path, fragment: str):
    """Verifica que los archivos de rutas no contengan fragmentos hardcodeados antiguos."""
    assert file_path.is_file(), f"El archivo objetivo no existe: {file_path}"
    content = file_path.read_text(encoding="utf-8")
    assert fragment not in content, f"Se encontró el fragmento prohibido '{fragment}' en {file_path.name}"
