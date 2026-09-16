"""Pruebas unitarias para Story 1.4: Canal y Bot de Telegram para Alertas Técnicas a Soporte TI."""

import os
import json
import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import Base, User, Business, DIANExtractionJob
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.queue.worker import ExtractionWorker
from dian_automation.queue.exceptions import MailTokenTimeoutError
from dian_automation.telegram.tech_ops_bot import (
    TechOpsAlertBot,
    create_tech_ops_on_failure_callback,
)


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria para pruebas de alertas de Telegram."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Sembrar usuarios tri-rol
    db = TestingSessionLocal()
    devops_user = User(
        id="usr-tech-1",
        email="devops@kontable.co",
        full_name="Alex TechOps",
        role="TECH_OPS",
        telegram_chat_id=987654321,
        is_active=True,
    )
    inactive_tech = User(
        id="usr-tech-2",
        email="inactive@kontable.co",
        full_name="Inactive DevOps",
        role="TECH_OPS",
        telegram_chat_id=11223344,
        is_active=False,
    )
    admin_user = User(
        id="usr-admin-1",
        email="katerinn@kontable.co",
        full_name="Katerinn",
        role="ADMIN",
        telegram_chat_id=55566677,
        is_active=True,
    )
    client_user = User(
        id="usr-client-1",
        email="andrea@torres.co",
        full_name="Andrea Torres",
        role="CLIENT",
        telegram_chat_id=99988877,
        is_active=True,
    )

    biz = Business(
        id="biz-tech-1",
        client_id=client_user.id,
        legal_name="Andrea Torres Diseño SAS",
        commercial_name="Torres Branding",
        nit="901888999",
        dv="4",
    )

    db.add_all([devops_user, inactive_tech, admin_user, client_user, biz])
    db.commit()
    db.close()

    return TestingSessionLocal


def test_tech_ops_recipients_filtering(db_session_factory):
    """Verifica que solo los usuarios activos con rol TECH_OPS y telegram_chat_id sean destinatarios."""
    db = db_session_factory()
    try:
        bot = TechOpsAlertBot(bot_token="test_token")
        chat_ids = bot.get_tech_ops_chat_ids(db)
        # Solo usr-tech-1 debe ser incluido (admin, client e inactive excluidos)
        assert chat_ids == [987654321]
    finally:
        db.close()


def test_alert_formatting():
    """Verifica el formato del mensaje con todos los campos requeridos en Markdown."""
    msg = TechOpsAlertBot.format_alert_message(
        business_name="Torres Branding",
        nit="901888999-4",
        period="2026-08",
        error_code="MAIL_TIMEOUT",
        error_detail="IMAP timeout buscando token Stalwart",
        attempt_count=2,
        max_attempts=3,
        elapsed_seconds=14.5,
    )
    assert "Torres Branding" in msg
    assert "901888999-4" in msg
    assert "2026-08" in msg
    assert "MAIL_TIMEOUT" in msg
    assert "IMAP timeout buscando token Stalwart" in msg
    assert "2/3" in msg
    assert "14.5s" in msg
    assert "Reintentar" in msg or "reanudación" in msg


def test_send_alert_with_screenshot(db_session_factory, tmp_path):
    """Verifica el despacho mediante sendPhoto cuando existe captura de pantalla."""
    db = db_session_factory()
    screenshot_file = str(tmp_path / "screenshot_espera.png")
    with open(screenshot_file, "w") as f:
        f.write("fake-screenshot-content")

    dispatched_calls = []

    def mock_dispatcher(endpoint, data, files):
        dispatched_calls.append({"endpoint": endpoint, "data": data, "files": files})
        return {"ok": True, "result": {"message_id": 101}}

    try:
        job = ExtractionQueueManager.enqueue_job("biz-tech-1", "2026-08", db)
        job = ExtractionQueueManager.mark_job_failed(
            job_id=job.id,
            error_code="TURNSTILE_BLOCKED",
            error_detail="Cloudflare Turnstile token verification failed",
            screenshot_path=screenshot_file,
            db=db,
        )

        bot = TechOpsAlertBot(bot_token="test_token", http_dispatcher=mock_dispatcher)
        result = bot.send_failure_alert(job=job, db=db)

        assert result["sent"] is True
        assert result["endpoint"] == "sendPhoto"
        assert result["has_screenshot"] is True
        assert len(dispatched_calls) == 1

        call = dispatched_calls[0]
        assert call["endpoint"] == "sendPhoto"
        assert call["data"]["chat_id"] == 987654321
        assert "TURNSTILE_BLOCKED" in call["data"]["caption"]
        assert call["files"]["photo"] == screenshot_file

        # Verificar botón inline de reintento
        reply_markup = json.loads(call["data"]["reply_markup"])
        btn = reply_markup["inline_keyboard"][0][0]
        assert btn["text"] == "🔄 Reintentar Ahora"
        assert btn["callback_data"] == f"retry:{job.id}"

    finally:
        db.close()


def test_send_alert_without_screenshot_fallback(db_session_factory):
    """Verifica que si no hay captura, se envíe como mensaje de texto plano con sendMessage."""
    db = db_session_factory()
    dispatched_calls = []

    def mock_dispatcher(endpoint, data, files):
        dispatched_calls.append({"endpoint": endpoint, "data": data, "files": files})
        return {"ok": True, "result": {"message_id": 102}}

    try:
        job = ExtractionQueueManager.enqueue_job("biz-tech-1", "2026-08", db)
        job = ExtractionQueueManager.mark_job_failed(
            job_id=job.id,
            error_code="DIAN_DOWN",
            error_detail="Portal DIAN 503 Mantenimiento",
            screenshot_path=None,
            db=db,
        )

        bot = TechOpsAlertBot(bot_token="test_token", http_dispatcher=mock_dispatcher)
        result = bot.send_failure_alert(job=job, db=db)

        assert result["sent"] is True
        assert result["endpoint"] == "sendMessage"
        assert result["has_screenshot"] is False
        assert len(dispatched_calls) == 1

        call = dispatched_calls[0]
        assert call["endpoint"] == "sendMessage"
        assert "DIAN_DOWN" in call["data"]["text"]
        assert call["files"] is None
    finally:
        db.close()


def test_handle_retry_callback(db_session_factory):
    """Verifica que el callback de reintento adelante next_run_at a now y reactive el job."""
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job("biz-tech-1", "2026-08", db)
        # Simular fallo terminal (status FAILED, next_run_at +1 hora)
        job = ExtractionQueueManager.mark_job_failed(
            job_id=job.id,
            error_code="EXPORT_TIMEOUT",
            error_detail="Timeout exportando",
            db=db,
            backoff_seconds=3600,
        )
        job.status = "FAILED"
        db.commit()

        # El operador presiona [ 🔄 Reintentar Ahora ]
        callback_data = f"retry:{job.id}"
        response = TechOpsAlertBot.handle_retry_callback(callback_data, db)

        assert response["success"] is True
        assert "reprogramada" in response["message"]

        db.refresh(job)
        assert job.status == "ENQUEUED"
        # Debe poder ejecutarse inmediatamente
        assert job.next_run_at <= datetime.utcnow()

        # Verificar que el orquestador ahora sí lo toma como siguiente ejecutable
        runnable = ExtractionQueueManager.get_next_runnable_job(db)
        assert runnable is not None
        assert runnable.id == job.id
    finally:
        db.close()


def test_worker_integration_with_tech_ops_callback(db_session_factory):
    """Verifica que al fallar el worker, se dispare automáticamente la alerta a Tech-Ops."""
    db = db_session_factory()
    dispatched_alerts = []

    def mock_dispatcher(endpoint, data, files):
        dispatched_alerts.append({"endpoint": endpoint, "data": data})
        return {"ok": True}

    try:
        job = ExtractionQueueManager.enqueue_job("biz-tech-1", "2026-08", db)
        bot = TechOpsAlertBot(bot_token="test_token", http_dispatcher=mock_dispatcher)
        on_failure = create_tech_ops_on_failure_callback(bot=bot, db_session_factory=db_session_factory)

        def failing_extractor(biz, period):
            raise MailTokenTimeoutError("Error IMAP de prueba")

        worker = ExtractionWorker(db_session_factory=db_session_factory)
        res = worker.execute_job(
            job_id=job.id,
            extractor_func=failing_extractor,
            on_failure_callback=on_failure,
        )

        assert res["status"] == "RETRY_SCHEDULED"
        assert len(dispatched_alerts) == 1
        assert "MAIL_TIMEOUT" in (dispatched_alerts[0]["data"].get("text") or dispatched_alerts[0]["data"].get("caption"))
    finally:
        db.close()
