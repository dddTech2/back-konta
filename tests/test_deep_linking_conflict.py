"""Pruebas de conflicto y resolución de Telegram chat_id duplicado en Deep Linking."""

import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

from dian_automation.db.models import Base, User, Business, TelegramLinkToken
from dian_automation.telegram.deep_linking import TelegramDeepLinkingService


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria para pruebas de conflicto de vinculación."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return TestingSessionLocal


def test_process_start_payload_conflict_when_chat_id_already_linked(db_session_factory):
    """Cuando un chat_id ya está vinculado a otro usuario, process_start_payload rechaza la solicitud,
    mantiene el token sin usar y el usuario sin vincular. Tras liberar el chat, el mismo token vincula exitosamente."""
    db = db_session_factory()
    try:
        shared_chat_id = 987654321

        # Usuario 1 (ya vinculado con ese chat_id)
        user1 = User(
            id="usr-client-existing",
            email="existing@empresa.co",
            full_name="Usuario Existente",
            role="CLIENT",
            telegram_chat_id=shared_chat_id,
            telegram_username="existente",
            is_telegram_linked=True,
            is_active=True,
        )
        # Usuario 2 (nuevo cliente que intenta vincularse con ese mismo chat_id)
        user2 = User(
            id="usr-client-new",
            email="nuevo@empresa.co",
            full_name="Usuario Nuevo",
            role="CLIENT",
            telegram_chat_id=None,
            telegram_username=None,
            is_telegram_linked=False,
            is_active=True,
        )
        biz2 = Business(
            id="biz-new",
            client_id=user2.id,
            legal_name="Nueva Empresa SAS",
            commercial_name="Nueva Empresa",
            nit="901555444",
            dv="1",
        )
        db.add_all([user1, user2, biz2])
        db.commit()

        # Generar token para Usuario 2
        token_obj, _ = TelegramDeepLinkingService.generate_link_token(user2.id, db)
        assert token_obj.is_used is False

        # 1. Intento de vinculación con chat_id ya ocupado
        res_conflict = TelegramDeepLinkingService.process_start_payload(
            chat_id=shared_chat_id,
            payload=token_obj.token,
            db=db,
            telegram_username="nuevo_telegram",
        )

        assert res_conflict["success"] is False
        assert res_conflict["reason"] == "CHAT_ALREADY_LINKED"
        assert "Este Telegram ya está asociado a otra cuenta de Kontable" in res_conflict["message"]
        assert "libere" in res_conflict["message"]

        # Verificar que el token sigue sin consumir
        db.refresh(token_obj)
        assert token_obj.is_used is False
        assert token_obj.used_at is None

        # Verificar que el Usuario 2 no fue modificado
        db.refresh(user2)
        assert user2.telegram_chat_id is None
        assert user2.is_telegram_linked is False

        # 2. Se libera el chat_id del Usuario 1
        user1.telegram_chat_id = None
        user1.telegram_username = None
        user1.is_telegram_linked = False
        db.commit()

        # 3. El Usuario 2 vuelve a intentar con el MISMO token
        res_retry = TelegramDeepLinkingService.process_start_payload(
            chat_id=shared_chat_id,
            payload=token_obj.token,
            db=db,
            telegram_username="nuevo_telegram",
        )

        assert res_retry["success"] is True
        assert res_retry["chat_id"] == shared_chat_id

        # Verificar que Usuario 2 ahora sí está vinculado
        db.refresh(user2)
        assert user2.telegram_chat_id == shared_chat_id
        assert user2.telegram_username == "nuevo_telegram"
        assert user2.is_telegram_linked is True

        # Verificar que el token quedó consumido
        db.refresh(token_obj)
        assert token_obj.is_used is True
        assert token_obj.used_at is not None
    finally:
        db.close()


def test_process_start_payload_integrity_error_handled(db_session_factory, monkeypatch):
    """En caso de condición de carrera donde el commit lance IntegrityError, se hace rollback y se devuelve CHAT_ALREADY_LINKED."""
    db = db_session_factory()
    try:
        user = User(
            id="usr-client-race",
            email="race@empresa.co",
            full_name="Usuario Carrera",
            role="CLIENT",
            telegram_chat_id=None,
            is_telegram_linked=False,
            is_active=True,
        )
        db.add(user)
        db.commit()

        token_obj, _ = TelegramDeepLinkingService.generate_link_token(user.id, db)

        # Simular que db.commit lanza IntegrityError en el momento de vincular
        def failing_commit():
            raise IntegrityError("statement", "params", "orig")

        monkeypatch.setattr(db, "commit", failing_commit)

        res = TelegramDeepLinkingService.process_start_payload(
            chat_id=555666777,
            payload=token_obj.token,
            db=db,
        )

        assert res["success"] is False
        assert res["reason"] == "CHAT_ALREADY_LINKED"
        assert "Este Telegram ya está asociado a otra cuenta de Kontable" in res["message"]
    finally:
        db.close()
