"""Pruebas de validación estática de archivos de despliegue y Docker.

Verifica la configuración de PostgreSQL 16, respaldos automáticos,
variables de entorno, exclusiones en git/docker y scripts de shell.
"""

from pathlib import Path
import re
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent


def _extract_service_block(compose_text: str, service_name: str) -> str:
    """Extrae las líneas de un servicio en docker-compose.yml por indentación."""
    lines = compose_text.splitlines()
    block: list[str] = []
    in_block = False
    target_prefix = f"  {service_name}:"

    for line in lines:
        if line.rstrip() == target_prefix:
            in_block = True
            block.append(line)
            continue
        if in_block:
            # Si encontramos otro servicio de nivel 1 (2 espacios y termina en :) o sección raíz (0 espacios)
            if re.match(r"^ {2}[a-zA-Z0-9_-]+:", line) or (line and not line.startswith(" ")):
                break
            block.append(line)

    return "\n".join(block)


def test_compose_declares_services_and_volume():
    """(a) docker-compose.yml declara postgres, backup y el volumen postgres_data."""
    compose_text = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    assert "  postgres:" in compose_text, "Servicio 'postgres:' no encontrado en docker-compose.yml"
    assert "  backup:" in compose_text, "Servicio 'backup:' no encontrado en docker-compose.yml"
    assert "  postgres_data:" in compose_text, "Volumen 'postgres_data:' no encontrado en docker-compose.yml"


def test_postgres_service_security_and_healthcheck():
    """(b) El servicio postgres no expone puertos (sin ports:) y define healthcheck con pg_isready."""
    compose_text = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    postgres_block = _extract_service_block(compose_text, "postgres")
    assert postgres_block, "Bloque del servicio postgres no pudo ser extraído"

    assert "ports:" not in postgres_block, "El servicio postgres no debe exponer puertos (ports:)"
    assert "healthcheck:" in postgres_block, "El servicio postgres debe tener healthcheck"
    assert "pg_isready" in postgres_block, "El healthcheck de postgres debe invocar pg_isready"


def test_service_healthy_count():
    """(c) service_healthy aparece al menos 5 veces (migrate, api, bot, scheduler, backup)."""
    compose_text = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    count = compose_text.count("service_healthy")
    assert count >= 5, f"Se esperaban al menos 5 referencias a service_healthy, encontradas: {count}"


def test_compose_database_url_and_no_literal_passwords():
    """(d) El compose contiene DATABASE_URL parametrizada y ninguna contraseña literal."""
    compose_text = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    expected_url = "postgresql+psycopg://kontable:${POSTGRES_PASSWORD}@postgres:5432/kontable"
    assert expected_url in compose_text, f"URL esperada no encontrada: {expected_url}"

    # Busca 'kontable:' seguido de algo que no sea '${' (contraseña en texto plano)
    literal_matches = re.findall(r"kontable:(?!\$\{)", compose_text)
    assert not literal_matches, f"Se encontró contraseña literal tras 'kontable:': {literal_matches}"


def test_shell_scripts_line_endings_and_backup_content():
    """(e) docker/entrypoint.sh y docker/pg-backup.sh usan LF y pg-backup.sh tiene pg_dump y --format=custom."""
    entrypoint_bytes = (ROOT_DIR / "docker" / "entrypoint.sh").read_bytes()
    backup_bytes = (ROOT_DIR / "docker" / "pg-backup.sh").read_bytes()

    assert b"\r" not in entrypoint_bytes, "docker/entrypoint.sh contiene retornos de carro (CRLF)"
    assert b"\r" not in backup_bytes, "docker/pg-backup.sh contiene retornos de carro (CRLF)"

    backup_text = backup_bytes.decode("utf-8")
    assert "pg_dump" in backup_text, "docker/pg-backup.sh debe usar pg_dump"
    assert "--format=custom" in backup_text, "docker/pg-backup.sh debe usar formato custom (--format=custom)"


def test_entrypoint_does_not_echo_database_url():
    """(f) docker/entrypoint.sh no tiene ningún echo que incluya DATABASE_URL."""
    entrypoint_text = (ROOT_DIR / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    for line in entrypoint_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo") and "DATABASE_URL" in stripped:
            pytest.fail(f"docker/entrypoint.sh imprime DATABASE_URL en echo: {line}")


def test_env_example_and_ignore_files():
    """(g) .env.example contiene POSTGRES_PASSWORD con marcador y backups/ está en .gitignore y .dockerignore."""
    env_example_text = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD=cambia_esto_por_una_clave_hex_larga" in env_example_text, (
        ".env.example debe contener POSTGRES_PASSWORD con el marcador requerido"
    )

    gitignore_text = (ROOT_DIR / ".gitignore").read_text(encoding="utf-8")
    assert "backups/" in gitignore_text, ".gitignore debe excluir la carpeta backups/"

    dockerignore_text = (ROOT_DIR / ".dockerignore").read_text(encoding="utf-8")
    assert "backups/" in dockerignore_text, ".dockerignore debe excluir la carpeta backups/"


def test_api_frontend_volume_uses_frontend_dir_variable():
    compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${FRONTEND_DIR:-" in compose and ":/frontend/dist:ro" in compose
    assert "../ProyectoDianFront:/frontend" not in compose  # la carpeta del front ya no se asume por nombre
