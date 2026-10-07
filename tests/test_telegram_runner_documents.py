"""Pruebas para el manejo de archivos en el runner de Telegram (cli/telegram_bot.py): AC #6."""

from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.cli.telegram_bot import TelegramBotRunner
from dian_automation.db.database import Base
from dian_automation.db.models import User, Business


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()

    admin = User(
        id="usr-admin", email="admin@test.co", full_name="Admin", role="ADMIN",
        telegram_chat_id=111, is_telegram_linked=True, is_active=True,
    )
    client = User(
        id="usr-client", email="client@test.co", full_name="Cliente", role="CLIENT",
        telegram_chat_id=222, is_telegram_linked=True, is_active=True,
    )
    unlinked = User(
        id="usr-unlinked", email="unlinked@test.co", full_name="No Vinculado", role="CLIENT",
        telegram_chat_id=None, is_telegram_linked=False, is_active=True,
    )
    session.add_all([admin, client, unlinked])
    session.commit()

    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_runner_dispatches_admin_document(db_session, monkeypatch):
    mock_downloader = MagicMock(return_value=b"%PDF-sample")
    runner = TelegramBotRunner(bot_token="fake-token", file_downloader=mock_downloader)

    monkeypatch.setattr("dian_automation.cli.telegram_bot.SessionLocal", lambda: db_session)
    mock_send = MagicMock()
    monkeypatch.setattr(runner, "send_message", mock_send)

    update = {
        "message": {
            "chat": {"id": 111},
            "from": {"username": "admin_user", "first_name": "Admin"},
            "caption": "/subir_documento 901008579 RUT",
            "document": {"file_id": "file_123", "file_name": "rut.pdf", "file_size": 1024},
        }
    }

    with patch("dian_automation.telegram.admin_bot.AdminTelegramBot.handle_admin_document", return_value="OK confirmacion") as mock_handler:
        runner.handle_update(update)
        assert mock_handler.called
        assert mock_handler.call_args[1]["sender_chat_id"] == 111
        mock_send.assert_called_with(111, "OK confirmacion")


def test_runner_linked_client_file_is_rejected_without_downloading(db_session, monkeypatch):
    mock_downloader = MagicMock()
    runner = TelegramBotRunner(bot_token="fake-token", file_downloader=mock_downloader)

    monkeypatch.setattr("dian_automation.cli.telegram_bot.SessionLocal", lambda: db_session)
    mock_send = MagicMock()
    monkeypatch.setattr(runner, "send_message", mock_send)

    update = {
        "message": {
            "chat": {"id": 222},
            "from": {"username": "cliente_user", "first_name": "Cliente"},
            "document": {"file_id": "file_client", "file_name": "doc.pdf", "file_size": 1024},
        }
    }

    runner.handle_update(update)

    # NO debe descargar nada
    mock_downloader.assert_not_called()
    # Debe responder el mensaje exacto de rechazo
    mock_send.assert_called_once()
    sent_text = mock_send.call_args[0][1]
    assert "Por aquí no recibimos archivos. Envíaselos a Katerinn" in sent_text


def test_runner_unlinked_user_file_receives_unlinked_message(db_session, monkeypatch):
    mock_downloader = MagicMock()
    runner = TelegramBotRunner(bot_token="fake-token", file_downloader=mock_downloader)

    monkeypatch.setattr("dian_automation.cli.telegram_bot.SessionLocal", lambda: db_session)
    mock_send = MagicMock()
    monkeypatch.setattr(runner, "send_message", mock_send)

    update = {
        "message": {
            "chat": {"id": 999999},  # Chat desconocido
            "from": {"username": "desconocido", "first_name": "Extraño"},
            "photo": [{"file_id": "photo_1", "file_size": 500}],
        }
    }

    runner.handle_update(update)

    mock_downloader.assert_not_called()
    mock_send.assert_called_once()
    sent_text = mock_send.call_args[0][1]
    assert "Cuenta no vinculada" in sent_text
