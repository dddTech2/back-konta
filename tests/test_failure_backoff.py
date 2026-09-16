"""Pruebas unitarias para Story 1.3: Detección de Fallos, Backoff de 1 Hora y Reprogramación de Cola."""

import os
import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import Base, User, Business, DIANExtractionJob
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.queue.worker import ExtractionWorker
from dian_automation.queue.exceptions import (
    DIANExtractionError,
    AuthFailedError,
    MailTokenTimeoutError,
    TurnstileBlockedError,
    DIANPortalDownError,
    ExportTimeoutError,
)


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria para tests del worker y backoff."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Sembrar usuario y negocio de prueba
    db = TestingSessionLocal()
    user = User(id="user-fail-1", email="andrea@torres.com", full_name="Andrea Torres", role="CLIENT")
    db.add(user)
    biz = Business(
        id="biz-fail-1",
        client_id=user.id,
        legal_name="Servicios Tecnológicos Kontable SAS",
        commercial_name="Kontable Tech",
        nit="901555666",
        dv="7",
        taxpayer_type="PERSONA_JURIDICA",
        legal_rep_doc="1020304050",
    )
    biz2 = Business(
        id="biz-fail-2",
        client_id=user.id,
        legal_name="Comercializadora Torres SAS",
        commercial_name="Torres Store",
        nit="901777888",
        dv="3",
        taxpayer_type="PERSONA_JURIDICA",
    )
    db.add_all([biz, biz2])
    db.commit()
    db.close()

    return TestingSessionLocal


def test_typed_exceptions_hierarchy():
    """Verifica que todas las excepciones tipificadas hereden de DIANExtractionError con sus códigos."""
    auth_err = AuthFailedError()
    assert isinstance(auth_err, DIANExtractionError)
    assert auth_err.code == "AUTH_FAILED"

    mail_err = MailTokenTimeoutError()
    assert isinstance(mail_err, DIANExtractionError)
    assert mail_err.code == "MAIL_TIMEOUT"

    turnstile_err = TurnstileBlockedError()
    assert isinstance(turnstile_err, DIANExtractionError)
    assert turnstile_err.code == "TURNSTILE_BLOCKED"

    down_err = DIANPortalDownError()
    assert isinstance(down_err, DIANExtractionError)
    assert down_err.code == "DIAN_DOWN"

    export_err = ExportTimeoutError()
    assert isinstance(export_err, DIANExtractionError)
    assert export_err.code == "EXPORT_TIMEOUT"


def test_job_failure_single_attempt_reprograms_with_1h_backoff(db_session_factory):
    """Al fallar con MailTokenTimeoutError, el job se reprograma +1 hora y attempt_count pasa a 1."""
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job(
            business_id="biz-fail-1",
            target_period="2026-03",
            db=db,
        )
        job_id = job.id
        assert job.attempt_count == 0
        assert job.status == "ENQUEUED"

        def failing_extractor(biz, period):
            raise MailTokenTimeoutError("No se recibió token en Stalwart IMAP tras 120s")

        worker = ExtractionWorker(db_session_factory=db_session_factory)
        now_before = datetime.utcnow()
        result = worker.run_once(extractor_func=failing_extractor)

        assert result is not None
        assert result["status"] == "RETRY_SCHEDULED"
        assert result["error_code"] == "MAIL_TIMEOUT"
        assert "No se recibió token en Stalwart" in result["error_detail"]
        assert result["attempt_count"] == 1

        db.refresh(job)
        assert job.status == "ENQUEUED"
        assert job.attempt_count == 1
        assert job.error_code == "MAIL_TIMEOUT"
        assert job.next_run_at >= now_before + timedelta(seconds=3590)

        # Concurrencia/Cola: verificar que get_next_runnable_job no lo toma de inmediato
        next_job = ExtractionQueueManager.get_next_runnable_job(db=db)
        assert next_job is None
    finally:
        db.close()


def test_global_queue_pause_on_failure(db_session_factory):
    """Cuando un job falla, toda la cola encolada se pausa por 1 hora completa."""
    db = db_session_factory()
    try:
        job1 = ExtractionQueueManager.enqueue_job(
            business_id="biz-fail-1",
            target_period="2026-01",
            db=db,
        )
        job2 = ExtractionQueueManager.enqueue_job(
            business_id="biz-fail-2",
            target_period="2026-02",
            db=db,
        )

        def failing_extractor(biz, period):
            raise DIANPortalDownError("Portal DIAN 503 Mantenimiento Programado")

        worker = ExtractionWorker(db_session_factory=db_session_factory)
        now_before = datetime.utcnow()
        worker.execute_job(job_id=job1.id, extractor_func=failing_extractor)

        db.refresh(job1)
        db.refresh(job2)

        # Job 1 reprogramado al final (+1 hora)
        assert job1.status == "ENQUEUED"
        assert job1.attempt_count == 1
        assert job1.next_run_at >= now_before + timedelta(seconds=3590)

        # Job 2 también pausado (+1 hora)
        assert job2.status == "ENQUEUED"
        assert job2.next_run_at >= now_before + timedelta(seconds=3590)

        # Ninguno de los dos está disponible para ejecución inmediata
        assert ExtractionQueueManager.get_next_runnable_job(db=db) is None
    finally:
        db.close()


def test_terminal_failed_state_after_max_attempts(db_session_factory):
    """Si un trabajo alcanza max_attempts (3), debe pasar a FAILED de forma terminal."""
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job(
            business_id="biz-fail-1",
            target_period="2026-03",
            db=db,
        )
        job_id = job.id
        assert job.max_attempts == 3

        worker = ExtractionWorker(db_session_factory=db_session_factory)

        def failing_extractor(biz, period):
            raise TurnstileBlockedError("Cloudflare Turnstile token no superado")

        # Intento 1
        worker.execute_job(job_id=job_id, extractor_func=failing_extractor)
        db.refresh(job)
        assert job.attempt_count == 1
        assert job.status == "ENQUEUED"

        # Simular paso del tiempo y forzar siguiente ejecución (Intento 2)
        job.next_run_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
        worker.execute_job(job_id=job_id, extractor_func=failing_extractor)
        db.refresh(job)
        assert job.attempt_count == 2
        assert job.status == "ENQUEUED"

        # Simular paso del tiempo y forzar intento final (Intento 3)
        job.next_run_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
        result = worker.execute_job(job_id=job_id, extractor_func=failing_extractor)
        db.refresh(job)

        # Estado terminal
        assert result["status"] == "FAILED"
        assert job.attempt_count == 3
        assert job.status == "FAILED"
        assert job.error_code == "TURNSTILE_BLOCKED"

        # Ya no debe ser elegible para ejecución
        job.next_run_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
        assert ExtractionQueueManager.get_next_runnable_job(db=db) is None
    finally:
        db.close()


def test_screenshot_evidence_preservation(db_session_factory):
    """Verifica que la captura de pantalla de evidencia se copie a downloads/evidence_{job_id}.png."""
    db = db_session_factory()
    dummy_screenshot = "screenshot_espera.png"
    screenshot_path = None
    try:
        job = ExtractionQueueManager.enqueue_job(
            business_id="biz-fail-1",
            target_period="2026-03",
            db=db,
        )
        job_id = job.id

        # Crear captura temporal simulada
        with open(dummy_screenshot, "w") as f:
            f.write("fake-image-bytes-evidence")

        def failing_extractor(biz, period):
            raise ExportTimeoutError("Tiempo de espera agotado esperando reporte en DIAN")

        worker = ExtractionWorker(db_session_factory=db_session_factory)
        result = worker.execute_job(job_id=job_id, extractor_func=failing_extractor)

        db.refresh(job)
        assert job.screenshot_path is not None
        assert f"evidence_{job.id}.png" in job.screenshot_path
        assert os.path.exists(job.screenshot_path)
        screenshot_path = job.screenshot_path

        with open(job.screenshot_path, "r") as f:
            content = f.read()
        assert content == "fake-image-bytes-evidence"

    finally:
        if os.path.exists(dummy_screenshot):
            os.remove(dummy_screenshot)
        if screenshot_path and os.path.exists(screenshot_path):
            os.remove(screenshot_path)
        db.close()


def test_on_failure_callback_triggered(db_session_factory):
    """Verifica que el callback on_failure se invoque con los parámetros correctos para alertas."""
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job(
            business_id="biz-fail-1",
            target_period="2026-03",
            db=db,
        )
        job_id = job.id

        callback_calls = []

        def failure_listener(job_obj, err_code, err_detail, screen_path):
            callback_calls.append({
                "job_id": job_obj.id,
                "error_code": err_code,
                "error_detail": err_detail,
                "screenshot_path": screen_path,
            })

        def failing_extractor(biz, period):
            raise AuthFailedError("Credenciales o certificado DIAN revocado")

        worker = ExtractionWorker(db_session_factory=db_session_factory)
        worker.execute_job(
            job_id=job_id,
            extractor_func=failing_extractor,
            on_failure_callback=failure_listener,
        )

        assert len(callback_calls) == 1
        assert callback_calls[0]["job_id"] == job_id
        assert callback_calls[0]["error_code"] == "AUTH_FAILED"
        assert "Credenciales o certificado" in callback_calls[0]["error_detail"]
    finally:
        db.close()
