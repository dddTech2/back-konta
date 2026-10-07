"""Pruebas unitarias para telegram/notify.py (Story 8.1 - AC #5)."""

import httpx
import pytest

from dian_automation.telegram.notify import send_telegram_message


class DummyResponse:
    def __init__(self, status_code: int, data: dict):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


def test_send_telegram_message_success(monkeypatch):
    """Envío exitoso de mensaje con Markdown."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-123")
    calls = []

    def mock_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return DummyResponse(200, {"ok": True, "result": {"message_id": 101}})

    monkeypatch.setattr(httpx, "post", mock_post)

    result = send_telegram_message(123456, "Hola *mundo*", parse_mode="Markdown")
    assert result is True
    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.telegram.org/botfake-token-123/sendMessage"
    assert calls[0]["json"] == {
        "chat_id": 123456,
        "text": "Hola *mundo*",
        "parse_mode": "Markdown",
    }


def test_send_telegram_message_markdown_fails_retries_plain_text(monkeypatch, caplog):
    """Si Telegram rechaza el Markdown, reintenta en texto plano sin parse_mode y no loguea el token."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret-token")
    calls = []

    def mock_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if len(calls) == 1:
            return DummyResponse(400, {"ok": False, "description": "Bad Request: can't parse entities"})
        return DummyResponse(200, {"ok": True, "result": {"message_id": 102}})

    monkeypatch.setattr(httpx, "post", mock_post)

    result = send_telegram_message(987654, "Mensaje con *Markdown roto", parse_mode="Markdown")
    assert result is True
    assert len(calls) == 2
    # El primer intento incluye parse_mode
    assert calls[0]["json"]["parse_mode"] == "Markdown"
    # El segundo intento es en texto plano (sin parse_mode)
    assert "parse_mode" not in calls[1]["json"]
    assert calls[1]["json"] == {"chat_id": 987654, "text": "Mensaje con *Markdown roto"}

    # Seguridad: nunca se debe loguear el token
    assert "super-secret-token" not in caplog.text


def test_send_telegram_message_both_fail(monkeypatch):
    """Si ambos intentos fallan, retorna False."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-123")

    def mock_post(url, **kwargs):
        return DummyResponse(400, {"ok": False, "description": "Bad Request"})

    monkeypatch.setattr(httpx, "post", mock_post)

    result = send_telegram_message(123456, "Mensaje", parse_mode="Markdown")
    assert result is False


def test_send_telegram_message_network_error(monkeypatch):
    """Manejo seguro de excepciones de red."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-123")

    def mock_post(url, **kwargs):
        raise httpx.ConnectError("No route to host")

    monkeypatch.setattr(httpx, "post", mock_post)

    result = send_telegram_message(123456, "Mensaje")
    assert result is False


def test_send_telegram_message_missing_token(monkeypatch):
    """Sin token configurado, retorna False inmediatamente."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CLIENT_BOT_TOKEN", raising=False)

    result = send_telegram_message(123456, "Mensaje")
    assert result is False


def test_send_telegram_message_no_parse_mode_no_retry(monkeypatch):
    """Sin parse_mode, si falla no hay reintento."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-123")
    calls = []

    def mock_post(url, **kwargs):
        calls.append(kwargs)
        return DummyResponse(500, {"ok": False})

    monkeypatch.setattr(httpx, "post", mock_post)

    result = send_telegram_message(123456, "Texto plano", parse_mode=None)
    assert result is False
    assert len(calls) == 1
    assert "parse_mode" not in calls[0]["json"]


def test_send_telegram_message_fallback_env_tokens(monkeypatch):
    """Admite TELEGRAM_CLIENT_BOT_TOKEN o TELEGRAM_ADMIN_BOT_TOKEN si TELEGRAM_BOT_TOKEN no está."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CLIENT_BOT_TOKEN", "client-token-999")
    calls = []

    def mock_post(url, **kwargs):
        calls.append(url)
        return DummyResponse(200, {"ok": True})

    monkeypatch.setattr(httpx, "post", mock_post)

    result = send_telegram_message(123456, "Hola")
    assert result is True
    assert "client-token-999" in calls[0]
