"""Pruebas de seguridad: /hacerme_admin no debe permitir auto-escalación de privilegios
cuando hay una allowlist de chat_id configurada (ADMIN_BOOTSTRAP_CHAT_IDS / ADMIN_TELEGRAM_CHAT_ID)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.database import Base
from dian_automation.db.models import User

from dian_automation.cli import telegram_bot as bot_runner_module


@pytest.fixture
def isolated_session_factory(monkeypatch):
    """Reemplaza el SessionLocal usado por el runner por una base SQLite en memoria aislada."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(bot_runner_module, "SessionLocal", TestingSessionLocal)
    return TestingSessionLocal


def _make_runner(monkeypatch, allowlist_env: str = "", admin_chat_id_env: str = ""):
    monkeypatch.setenv("ADMIN_BOOTSTRAP_CHAT_IDS", allowlist_env)
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", admin_chat_id_env)
    runner = bot_runner_module.TelegramBotRunner(bot_token="fake-token")
    sent = []
    monkeypatch.setattr(runner, "send_message", lambda chat_id, text, parse_mode="Markdown": sent.append((chat_id, text)) or True)
    return runner, sent


def _fake_update(chat_id: int, text: str) -> dict:
    return {"message": {"chat": {"id": chat_id}, "text": text, "from": {"id": chat_id, "first_name": "Tester", "username": "tester"}}}


def test_hacerme_admin_rejected_when_no_allowlist_configured(isolated_session_factory, monkeypatch):
    """Sin allowlist configurada, /hacerme_admin queda rechazado por defecto y no crea ningún ADMIN."""
    runner, sent = _make_runner(monkeypatch, allowlist_env="", admin_chat_id_env="")
    runner.handle_update(_fake_update(111222333, "/hacerme_admin"))

    db = isolated_session_factory()
    try:
        user = db.query(User).filter(User.telegram_chat_id == 111222333).first()
        assert user is None, "Sin allowlist configurada no debe crearse ningún usuario ADMIN"
    finally:
        db.close()
    assert "No autorizado" in sent[-1][1]


def test_hacerme_admin_rejected_for_unlisted_chat_id(isolated_session_factory, monkeypatch):
    """Con una allowlist configurada, un chat_id fuera de ella NO puede auto-asignarse ADMIN."""
    runner, sent = _make_runner(monkeypatch, allowlist_env="999888777", admin_chat_id_env="")
    intruder_chat_id = 111222333

    runner.handle_update(_fake_update(intruder_chat_id, "/hacerme_admin"))

    db = isolated_session_factory()
    try:
        user = db.query(User).filter(User.telegram_chat_id == intruder_chat_id).first()
        assert user is None, "El chat_id no autorizado NO debe crear/promover ningún usuario ADMIN"
    finally:
        db.close()
    assert "No autorizado" in sent[-1][1]


def test_hacerme_admin_allowed_for_listed_chat_id(isolated_session_factory, monkeypatch):
    """El chat_id incluido en la allowlist sí puede auto-asignarse ADMIN."""
    allowed_chat_id = 999888777
    runner, sent = _make_runner(monkeypatch, allowlist_env=str(allowed_chat_id), admin_chat_id_env="")

    runner.handle_update(_fake_update(allowed_chat_id, "/hacerme_admin"))

    db = isolated_session_factory()
    try:
        user = db.query(User).filter(User.telegram_chat_id == allowed_chat_id).first()
        assert user is not None
        assert user.role == "ADMIN"
    finally:
        db.close()
    assert "Acceso de administración activado" in sent[-1][1]
    assert "COMANDOS DE ADMINISTRACIÓN COMERCIAL" in sent[-1][1]


def test_hacerme_admin_allowlist_via_admin_telegram_chat_id(isolated_session_factory, monkeypatch):
    """ADMIN_TELEGRAM_CHAT_ID por sí solo también cierra la puerta a cualquier otro chat_id."""
    runner, sent = _make_runner(monkeypatch, allowlist_env="", admin_chat_id_env="555444333")

    runner.handle_update(_fake_update(111222333, "/hacerme_admin"))

    db = isolated_session_factory()
    try:
        assert db.query(User).filter(User.telegram_chat_id == 111222333).first() is None
    finally:
        db.close()
    assert "No autorizado" in sent[-1][1]


def test_handle_update_unexpected_error_sanitized(isolated_session_factory, monkeypatch):
    """Ante un error inesperado, el mensaje enviado al usuario no filtra secretos ni SQL y es genérico."""
    runner, sent = _make_runner(monkeypatch, allowlist_env="", admin_chat_id_env="")

    def _failing_deep_link(*args, **kwargs):
        raise RuntimeError("secreto-sql-SELECT-PASSWORD-FROM-USERS psycopg error")

    monkeypatch.setattr(bot_runner_module.TelegramDeepLinkingService, "process_start_payload", _failing_deep_link)

    runner.handle_update(_fake_update(123456, "/start token_prueba_123"))

    assert len(sent) == 1
    reply = sent[-1][1]
    assert "secreto-sql" not in reply
    assert "psycopg" not in reply
    assert "SQL" not in reply
    assert "No pudimos procesar tu solicitud" in reply


def test_runner_messages_are_professional(isolated_session_factory, monkeypatch):
    """Los mensajes de /mi_id, Cuenta no vinculada y éxito de /hacerme_admin no contienen palabras técnicas o de prueba."""
    admin_id = 999111222
    runner, sent = _make_runner(monkeypatch, allowlist_env=str(admin_id), admin_chat_id_env="")

    # 1. /mi_id
    runner.handle_update(_fake_update(123456, "/mi_id"))
    msg_mi_id = sent[-1][1]

    # 2. Cuenta no vinculada
    runner.handle_update(_fake_update(123456, "hola"))
    msg_no_vinculada = sent[-1][1]

    # 3. Éxito de /hacerme_admin
    runner.handle_update(_fake_update(admin_id, "/hacerme_admin"))
    msg_admin_exito = sent[-1][1]

    prohibited = ["prueba", "local", ".env", "uv run", "terminal"]
    for msg in [msg_mi_id, msg_no_vinculada, msg_admin_exito]:
        msg_lower = msg.lower()
        for word in prohibited:
            assert word not in msg_lower, f"'{word}' encontrado en: {msg}"
