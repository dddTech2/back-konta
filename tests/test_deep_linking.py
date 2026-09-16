"""Pruebas unitarias para Story 2.1: Vinculación de Clientes por Deep Linking con Token Único."""

import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import Base, User, Business, TelegramLinkToken
from dian_automation.telegram.deep_linking import TelegramDeepLinkingService


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria para pruebas de Deep Linking."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Sembrar usuario y negocio de prueba
    db = TestingSessionLocal()
    client_user = User(
        id="usr-client-dl-1",
        email="andrea@torresbranding.co",
        full_name="Andrea Torres",
        role="CLIENT",
        telegram_chat_id=None,
        is_telegram_linked=False,
        is_active=True,
    )
    biz = Business(
        id="biz-dl-1",
        client_id=client_user.id,
        legal_name="Andrea Torres Diseño SAS",
        commercial_name="Torres Branding",
        nit="901888999",
        dv="4",
    )
    db.add_all([client_user, biz])
    db.commit()
    db.close()

    return TestingSessionLocal


def test_generate_deep_link_token(db_session_factory):
    """Verifica que el servicio genere tokens únicos seguros y URLs válidas de 72 horas."""
    db = db_session_factory()
    try:
        now_before = datetime.utcnow()
        token_obj, link_url = TelegramDeepLinkingService.generate_link_token(
            user_id="usr-client-dl-1",
            db=db,
            expires_in_hours=72,
            bot_username="KontableTestBot",
        )

        assert token_obj.id is not None
        assert token_obj.is_used is False
        assert len(token_obj.token) >= 32
        assert token_obj.expires_at >= now_before + timedelta(hours=71)
        assert link_url == f"https://t.me/KontableTestBot?start={token_obj.token}"

        # Verificar persistencia en DB
        record = db.query(TelegramLinkToken).filter(TelegramLinkToken.token == token_obj.token).first()
        assert record is not None
        assert record.user_id == "usr-client-dl-1"
    finally:
        db.close()


def test_process_start_payload_success(db_session_factory):
    """Verifica la vinculación atómica cuando el cliente envía /start <token> válido."""
    db = db_session_factory()
    try:
        token_obj, _ = TelegramDeepLinkingService.generate_link_token("usr-client-dl-1", db)
        chat_id = 9876543210

        result = TelegramDeepLinkingService.process_start_payload(
            chat_id=chat_id,
            payload=token_obj.token,
            db=db,
            telegram_username="andreatorres",
        )

        assert result["success"] is True
        assert result["user_id"] == "usr-client-dl-1"
        assert result["chat_id"] == chat_id
        assert "Andrea Torres" in result["welcome_message"]
        assert "Torres Branding" in result["welcome_message"]
        # El NIT se muestra tal cual quedó registrado, sin el DV calculado
        # internamente (el cliente nunca lo suministró).
        assert "901888999" in result["welcome_message"]
        assert "901888999-4" not in result["welcome_message"]

        # El mensaje de bienvenida incluye el enlace directo al dashboard web/móvil
        # con el NIT real, para la demo conectada front + Telegram.
        from dian_automation.config import config
        assert f"{config.kontable_web_url}?nit=901888999" in result["welcome_message"]

        # Verificar actualización del usuario en DB
        user = db.query(User).filter(User.id == "usr-client-dl-1").first()
        assert user.is_telegram_linked is True
        assert user.telegram_chat_id == chat_id
        assert user.telegram_username == "andreatorres"

        # Verificar que el token quedó marcado como consumido
        db.refresh(token_obj)
        assert token_obj.is_used is True
        assert token_obj.used_at is not None
    finally:
        db.close()


def test_token_cannot_be_reused(db_session_factory):
    """Un token ya consumido no puede volver a ser utilizado por nadie."""
    db = db_session_factory()
    try:
        token_obj, _ = TelegramDeepLinkingService.generate_link_token("usr-client-dl-1", db)
        chat_id = 9876543210

        # Primer uso exitoso
        res1 = TelegramDeepLinkingService.process_start_payload(chat_id, token_obj.token, db)
        assert res1["success"] is True

        # Segundo intento con el mismo token
        res2 = TelegramDeepLinkingService.process_start_payload(chat_id, token_obj.token, db)
        assert res2["success"] is False
        assert res2["reason"] == "TOKEN_ALREADY_USED"
        assert "ya fue utilizado" in res2["message"]
    finally:
        db.close()


def test_token_expired_rejected(db_session_factory):
    """Un token con más de 72 horas (expirado) debe ser rechazado."""
    db = db_session_factory()
    try:
        token_obj, _ = TelegramDeepLinkingService.generate_link_token("usr-client-dl-1", db)
        # Forzar expiración
        token_obj.expires_at = datetime.utcnow() - timedelta(minutes=5)
        db.commit()

        result = TelegramDeepLinkingService.process_start_payload(12345, token_obj.token, db)
        assert result["success"] is False
        assert result["reason"] == "TOKEN_EXPIRED"
        assert "expirado" in result["message"]

        # El usuario no debe quedar vinculado
        user = db.query(User).filter(User.id == "usr-client-dl-1").first()
        assert user.is_telegram_linked is False
    finally:
        db.close()


def test_token_not_found_or_empty(db_session_factory):
    """Manejo seguro de tokens vacíos o no existentes."""
    db = db_session_factory()
    try:
        # Token inexistente
        res_missing = TelegramDeepLinkingService.process_start_payload(12345, "token_inventado_123", db)
        assert res_missing["success"] is False
        assert res_missing["reason"] == "TOKEN_NOT_FOUND"

        # Token vacío
        res_empty = TelegramDeepLinkingService.process_start_payload(12345, "", db)
        assert res_empty["success"] is False
        assert res_empty["reason"] == "EMPTY_TOKEN"
    finally:
        db.close()


def test_handle_webhook_update_full_flow(db_session_factory):
    """Simula una llamada completa de webhook de Telegram procesando /start <token>."""
    db = db_session_factory()
    try:
        token_obj, _ = TelegramDeepLinkingService.generate_link_token("usr-client-dl-1", db)
        chat_id = 777888999

        replies = []

        def mock_sender(c_id, text):
            replies.append({"chat_id": c_id, "text": text})

        update_payload = {
            "update_id": 10001,
            "message": {
                "message_id": 1,
                "from": {"id": chat_id, "username": "andreatorres", "first_name": "Andrea"},
                "chat": {"id": chat_id, "type": "private"},
                "date": 1789400000,
                "text": f"/start {token_obj.token}",
            },
        }

        webhook_res = TelegramDeepLinkingService.handle_webhook_update(
            update=update_payload,
            db=db,
            sender_func=mock_sender,
        )

        assert webhook_res["status"] == "PROCESSED"
        assert webhook_res["command"] == "/start"
        assert webhook_res["result"]["success"] is True

        assert len(replies) == 1
        assert replies[0]["chat_id"] == chat_id
        assert "Andrea Torres" in replies[0]["text"]
        assert "Torres Branding" in replies[0]["text"]
    finally:
        db.close()
