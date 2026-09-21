"""Diagnóstico aislado: valida si Cloudflare Turnstile se resuelve solo o exige el checkbox.

No hace login ni descarga nada — solo abre el formulario de la DIAN (persona o empresa),
reutilizando el mismo lanzamiento de Chrome nativo + CDP que usa run_flow(), y observa
qué hace Turnstile. Sirve para probar esto de forma rápida y repetible sin correr todo
el flujo de extracción (login completo + espera de correo + descarga).

Uso:
    uv run python scripts/dev/check_turnstile.py --tipo persona
    uv run python scripts/dev/check_turnstile.py --tipo empresa --timeout 90
"""

import argparse
import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dian_automation.config import config
from dian_automation.dian_flow import launch_chrome_and_connect, wait_for_turnstile_ready


async def main(tipo: str, timeout_seconds: int) -> None:
    login_url = config.dian_company_url if tipo == "empresa" else config.dian_url
    print(f"Abriendo {login_url} ...")

    async with launch_chrome_and_connect(login_url) as (proc, browser, context, page):
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2000)

        result = await wait_for_turnstile_ready(page, timeout_seconds=timeout_seconds)

        print("\n" + "=" * 64)
        print("RESULTADO DEL DIAGNÓSTICO DE CLOUDFLARE TURNSTILE")
        print("=" * 64)
        print(f"¿Se resolvió (token generado)?     {'SÍ' if result.solved else 'NO'}")
        print(f"¿Se detectó el widget en pantalla?  {'SÍ' if result.widget_detected else 'NO'}")
        print(f"Tiempo transcurrido:                {result.elapsed_seconds:.1f}s de {timeout_seconds}s")
        print(f"Captura de evidencia:               {result.screenshot_path or '(ninguna)'}")
        print("=" * 64)

        if result.solved:
            print("\nTurnstile se resolvió sin intervención. El perfil actual está 'confiado'.")
        elif result.widget_detected:
            print(
                "\nCloudflare mostró el recuadro interactivo. Esto NO es un bug del código: "
                "es Cloudflare escalando el riesgo de la sesión (IP, frecuencia de intentos "
                "recientes, reputación del perfil). Si corres con HEADLESS=False, tienes 15s "
                "más para hacer clic ahora mismo y así dejar el perfil confiado para la próxima."
            )
            await page.wait_for_timeout(15000)
        else:
            print(
                "\nEl widget de Turnstile nunca llegó a cargar en pantalla. Revisa conectividad "
                "de red hacia el portal DIAN, o si la URL de login sigue siendo correcta."
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Valida si Cloudflare Turnstile pide el checkbox en el login de la DIAN."
    )
    parser.add_argument("--tipo", choices=["persona", "empresa"], default="persona")
    parser.add_argument("--timeout", type=int, default=60, help="Segundos a esperar (default 60)")
    args = parser.parse_args()

    asyncio.run(main(args.tipo, args.timeout))
