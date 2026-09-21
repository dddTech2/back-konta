"""Pruebas de verificación de identidad de marca (Konta)."""

from pathlib import Path
import re
from dian_automation.branding import BRAND_NAME, BRAND_TAGLINE
import dian_automation.api.app as api_app


def test_branding_constants():
    """Verifica los valores oficiales de las constantes de marca."""
    assert BRAND_NAME == "Konta"
    assert BRAND_TAGLINE == "Con K, contabilidad para emprendedores"


def test_app_docstring_and_branding():
    """Verifica que el docstring de la aplicación FastAPI use el nombre de marca Konta."""
    assert "Konta" in (api_app.__doc__ or "")


def test_no_kontable_literals_in_telegram_and_subscriptions():
    """Verifica que no queden literales de texto con 'Kontable' en telegram ni subscriptions."""
    base_dir = Path(__file__).resolve().parent.parent / "src" / "dian_automation"
    dirs_to_check = [base_dir / "telegram", base_dir / "subscriptions"]

    pattern = re.compile(r'["\'][^"\']*Kontable[^"\']*["\']')
    violations = []

    for d in dirs_to_check:
        for py_file in d.glob("*.py"):
            with open(py_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line_no, line in enumerate(lines, start=1):
                if any(excluded in line for excluded in ("@cliente.kontable.co", "@kontable.co", "kontable-worker")):
                    continue
                matches = pattern.findall(line)
                if matches:
                    violations.append(f"{py_file.name}:{line_no} -> {matches}")

    assert violations == [], f"Se encontraron literales no permitidos con 'Kontable': {violations}"
