"""Pruebas de seguridad: /hacerme_admin no debe permitir auto-escalación de privilegios
cuando hay una allowlist de chat_id configurada (ADMIN_BOOTSTRAP_CHAT_IDS / ADMIN_TELEGRAM_CHAT_ID)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.database import Base
from dian_automation.db.models import User

import run_telegram_bot as bot_runner_module


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


def test_hacerme_admin_open_when_no_allowlist_configured(isolated_session_factory, monkeypatch):
    """Sin ninguna variable de allowlist configurada, /hacerme_admin sigue abierto (conveniencia local)."""
    runner, sent = _make_runner(monkeypatch, allowlist_env="", admin_chat_id_env="")
    runner.handle_update(_fake_update(111222333, "/hacerme_admin"))

    db = isolated_session_factory()
    try:
        user = db.query(User).filter(User.telegram_chat_id == 111222333).first()
        assert user is not None
        assert user.role == "ADMIN"
    finally:
        db.close()
    assert "Privilegios de Administradora" in sent[-1][1]


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
    assert "Privilegios de Administradora" in sent[-1][1]


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
