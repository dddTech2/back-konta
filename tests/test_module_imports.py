"""Cada módulo de entrada se importa solo en un proceso limpio, sin depender del orden de la suite.

Regresión de la Story 7.2: `auth_service` importa `client_bot` (envío del OTP); si `client_bot` o
`deep_linking` importan `auth_service` a nivel de módulo, el bot se cae al arrancar con un import circular
que la suite no detecta porque otro test ya cargó los módulos en el orden "bueno".
"""

import subprocess
import sys

import pytest

MODULES = [
    "dian_automation.telegram.client_bot",
    "dian_automation.telegram.deep_linking",
    "dian_automation.telegram.admin_bot",
    "dian_automation.core.auth_service",
    "dian_automation.cli.telegram_bot",
    "dian_automation.api.app",
    "dian_automation.core.admin_service",
    "dian_automation.telegram.notify",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_in_a_fresh_process(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
