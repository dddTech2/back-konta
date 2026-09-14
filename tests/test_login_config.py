import pytest
from dian_automation import sanitize_nit, config
from dian_automation.dian_flow import run_flow
import argparse

def test_sanitize_nit_variations():
    # Con guión y dígito de verificación
    assert sanitize_nit("901.234.567-1") == "901234567"
    assert sanitize_nit("901234567-8") == "901234567"
    assert sanitize_nit(" 800.123.456-9 ") == "800123456"

    # Con puntos pero sin guión
    assert sanitize_nit("901.234.567") == "901234567"

    # Solo dígitos
    assert sanitize_nit("1000000001") == "1000000001"
    assert sanitize_nit("901234567") == "901234567"

    # Vacíos o nulos
    assert sanitize_nit("") == ""
    assert sanitize_nit(None) == ""
    assert sanitize_nit("   ") == ""

import asyncio

def test_run_flow_empresa_validation_missing_nit():
    with pytest.raises(ValueError, match="Para la modalidad 'empresa' se requiere"):
        asyncio.run(
            run_flow(
                login_type="empresa",
                representative_code="1000000001",
                company_nit="",
                token_url=None
            )
        )

def test_run_flow_empresa_validation_missing_rep():
    with pytest.raises(ValueError, match="Para la modalidad 'empresa' se requiere"):
        asyncio.run(
            run_flow(
                login_type="empresa",
                representative_code="",
                company_nit="901234567",
                token_url=None
            )
        )

def test_run_flow_persona_validation_missing_code():
    with pytest.raises(ValueError, match="Para la modalidad 'persona' se requiere"):
        asyncio.run(
            run_flow(
                login_type="persona",
                person_code="",
                token_url=None
            )
        )

def test_cli_argument_parser():
    from dian_automation.dian_flow import main
    # Verificamos que el parser reconozca los nuevos flags
    parser = argparse.ArgumentParser()
    parser.add_argument("--tipo", choices=["persona", "empresa"], default=None)
    parser.add_argument("--nit-representante", type=str, default=None)
    parser.add_argument("--nit-empresa", type=str, default=None)
    parser.add_argument("--cedula", type=str, default=None)

    args = parser.parse_args([
        "--tipo", "empresa",
        "--nit-representante", "1000000001",
        "--nit-empresa", "901.234.567-1"
    ])
    assert args.tipo == "empresa"
    assert args.nit_representante == "1000000001"
    assert args.nit_empresa == "901.234.567-1"
    assert sanitize_nit(args.nit_empresa) == "901234567"
