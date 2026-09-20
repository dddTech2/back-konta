"""Pruebas de la Story 1.6: reintento a 6 h de descargas lentas, recuperación de trabajos atascados
y aviso a la administradora."""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import Base, User, Business, DIANExtractionJob
from dian_automation.queue.exceptions import (
    AuthFailedError,
    ExportTimeoutError,
    STALE_PROCESSING_CODE,
    is_slow_error,
)
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.queue.worker import ExtractionWorker
from dian_automation.telegram.admin_alerts import (
    create_failure_alert_callback,
    notify_admins,
    to_bogota_text,
)
from dian_automation.telegram.tech_ops_bot import TechOpsAlertBot

ADMIN_CHATS = {1001, 1002}
TECH_CHAT = 2001
SLOW_SECONDS = ExtractionQueueManager.SLOW_RETRY_SECONDS
HARD_SECONDS = ExtractionQueueManager.FAILURE_BACKOFF_SECONDS


@pytest.fixture
def db_session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = factory()
    client = User(id="usr-client", email="cliente@test.com", full_name="Andrea Torres", role="CLIENT")
    db.add_all(
        [
            client,
            User(id="adm-1", email="a1@test.com", full_name="Katerinn", role="ADMIN", telegram_chat_id=1001),
            User(id="adm-2", email="a2@test.com", full_name="Suplente", role="ADMIN", telegram_chat_id=1002),
            User(id="adm-off", email="a3@test.com", full_name="Inactiva", role="ADMIN", telegram_chat_id=1003, is_active=False),
            User(id="adm-nochat", email="a4@test.com", full_name="Sin chat", role="ADMIN"),
            User(id="tech-1", email="t1@test.com", full_name="Alex", role="TECH_OPS", telegram_chat_id=TECH_CHAT),
        ]
    )
    db.add_all(
        [
            Business(
                id="biz-a", client_id=client.id, legal_name="Servicios Kontable SAS",
                commercial_name="Torres_Store *Tech*", nit="901555666", dv="7", taxpayer_type="PERSONA_JURIDICA",
            ),
            Business(
                id="biz-b", client_id=client.id, legal_name="Comercializadora Torres SAS",
                commercial_name="Torres Store", nit="901777888", dv="3", taxpayer_type="PERSONA_JURIDICA",
            ),
        ]
    )
    db.commit()
    db.close()
    return factory


class FakeTelegram:
    """Dispatcher de Telegram falso que registra cada envío."""

    def __init__(self, fail_with=None, result=None):
        self.calls = []
        self.fail_with = fail_with
        self.result = result if result is not None else {"ok": True}

    def __call__(self, endpoint, data, files):
        self.calls.append({"endpoint": endpoint, "data": data, "files": files})
        if self.fail_with:
            raise self.fail_with
        return self.result

    def chats(self):
        return {c["data"]["chat_id"] for c in self.calls}

    def texts_for(self, chat_id):
        return [c["data"].get("text") or c["data"].get("caption") for c in self.calls if c["data"]["chat_id"] == chat_id]


def _callback(db_session_factory, telegram):
    bot = TechOpsAlertBot(bot_token="123:TEST", http_dispatcher=telegram)
    return create_failure_alert_callback(bot=bot, db_session_factory=db_session_factory)


def _failing(exc):
    def extractor(business, period):
        raise exc

    return extractor


def _within(value, expected_seconds, tolerance=30):
    delta = (value - datetime.utcnow()).total_seconds()
    return expected_seconds - tolerance <= delta <= expected_seconds + tolerance


def _stale(db, job_id, seconds=5000):
    job = db.query(DIANExtractionJob).filter(DIANExtractionJob.id == job_id).one()
    job.status = "PROCESSING"
    job.started_at = datetime.utcnow() - timedelta(seconds=seconds)
    db.commit()
    return job


# --- Clasificación (AC #1, #2) ---------------------------------------------------------------


def test_slow_classification_covers_both_reporting_names():
    assert is_slow_error("ExportTimeoutError")  # worker remoto: type(e).__name__
    assert is_slow_error(ExportTimeoutError().code)  # worker local: e.code (EXPORT_TIMEOUT)
    assert is_slow_error(STALE_PROCESSING_CODE)


@pytest.mark.parametrize(
    "code",
    ["AuthFailedError", "AUTH_FAILED", "MAIL_TIMEOUT", "MailTokenTimeoutError", "TURNSTILE_BLOCKED",
     "DIANPortalDownError", "DIAN_DOWN", "TimeoutError", "ValueError", "", None],
)
def test_everything_else_is_a_hard_failure(code):
    assert is_slow_error(code) is False


def test_report_not_ready_raises_export_timeout_error(tmp_path):
    """dian_flow lanzaba TimeoutError, que nunca habría sido clasificado como lento."""
    from dian_automation.dian_flow import wait_and_download_export

    with pytest.raises(ExportTimeoutError) as excinfo:
        asyncio.run(
            wait_and_download_export(
                page=object(), start_fmt="01/03/2026", end_fmt="31/03/2026",
                timeout_seconds=0, download_dir=str(tmp_path),
            )
        )
    assert excinfo.value.code == "EXPORT_TIMEOUT"


# --- Reintento a 6 h (AC #1, #2, #4) ---------------------------------------------------------


def test_slow_failure_reschedules_only_that_job_six_hours(db_session_factory):
    db = db_session_factory()
    try:
        slow_job = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        other = ExtractionQueueManager.enqueue_job("biz-b", "2026-03", db)
        other_next_run = other.next_run_at

        failed = ExtractionQueueManager.mark_job_failed(slow_job.id, "ExportTimeoutError", "tardó", db)
        db.refresh(other)

        assert failed.status == "ENQUEUED"
        assert failed.attempt_count == 1
        assert failed.error_code == "ExportTimeoutError"
        assert _within(failed.next_run_at, SLOW_SECONDS)
        assert other.next_run_at == other_next_run  # ningún otro trabajo cambia
        assert ExtractionQueueManager.get_next_runnable_job(db).id == other.id
    finally:
        db.close()


def test_local_worker_slow_failure_uses_the_export_timeout_code(db_session_factory):
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        result = ExtractionWorker(db_session_factory).run_once(extractor_func=_failing(ExportTimeoutError("lento")))

        assert result["status"] == "RETRY_SCHEDULED"
        assert result["error_code"] == "EXPORT_TIMEOUT"
        db.refresh(job)
        assert job.status == "ENQUEUED" and _within(job.next_run_at, SLOW_SECONDS)
    finally:
        db.close()


def test_hard_failure_keeps_one_hour_and_pauses_the_rest(db_session_factory):
    db = db_session_factory()
    try:
        hard = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        other = ExtractionQueueManager.enqueue_job("biz-b", "2026-03", db)

        # Un código desconocido (incluida la TimeoutError genérica) es duro
        ExtractionQueueManager.mark_job_failed(hard.id, "TimeoutError", "otro tiempo", db)
        db.refresh(hard)
        db.refresh(other)

        assert _within(hard.next_run_at, HARD_SECONDS) and _within(other.next_run_at, HARD_SECONDS)
    finally:
        db.close()


def test_slow_failures_exhaust_max_attempts_into_failed_without_touching_others(db_session_factory):
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        other = ExtractionQueueManager.enqueue_job("biz-b", "2026-03", db)
        other_next_run = other.next_run_at

        for expected_attempt in (1, 2, 3):
            job = ExtractionQueueManager.mark_job_failed(job.id, "EXPORT_TIMEOUT", "lento", db)
            assert job.attempt_count == expected_attempt

        assert job.status == "FAILED"
        db.refresh(other)
        assert other.next_run_at == other_next_run
    finally:
        db.close()


def test_two_slow_failures_in_a_row_are_independent(db_session_factory):
    db = db_session_factory()
    try:
        first = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        second = ExtractionQueueManager.enqueue_job("biz-b", "2026-03", db)

        ExtractionQueueManager.mark_job_failed(first.id, "EXPORT_TIMEOUT", "lento", db)
        assert ExtractionQueueManager.get_next_runnable_job(db).id == second.id
        ExtractionQueueManager.mark_job_processing(second.id, db)
        ExtractionQueueManager.mark_job_failed(second.id, "EXPORT_TIMEOUT", "lento", db)

        db.refresh(first)
        db.refresh(second)
        assert _within(first.next_run_at, SLOW_SECONDS) and _within(second.next_run_at, SLOW_SECONDS)
        assert first.attempt_count == 1 and second.attempt_count == 1
    finally:
        db.close()


# --- Aviso a la administradora (AC #3, #4) ---------------------------------------------------


def test_bogota_time_conversion():
    assert to_bogota_text(datetime(2026, 9, 20, 15, 0)) == "20/09/2026 10:00"
    assert to_bogota_text(datetime(2026, 9, 21, 2, 30)) == "20/09/2026 21:30"  # cruza el día


def test_slow_failure_notifies_only_active_admins_with_plain_language(db_session_factory):
    telegram = FakeTelegram()
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        ExtractionWorker(db_session_factory).run_once(
            extractor_func=_failing(ExportTimeoutError("Traceback: selector #tableExport")),
            on_failure_callback=_callback(db_session_factory, telegram),
        )
        db.refresh(job)

        assert telegram.chats() == ADMIN_CHATS  # ni TECH_OPS, ni inactivos, ni sin chat
        text = telegram.texts_for(1001)[0]
        assert "Torres_Store *Tech*" in text
        assert "901555666-7" in text
        assert "2026-03" in text
        assert "el archivo quedó cargando en la DIAN" in text
        assert "Intento 1 de 3" in text
        assert to_bogota_text(job.next_run_at) in text
        assert "Traceback" not in text and "tableExport" not in text and "EXPORT_TIMEOUT" not in text
        assert all(c["endpoint"] == "sendMessage" and c["files"] is None for c in telegram.calls)
        assert all("parse_mode" not in c["data"] for c in telegram.calls)
    finally:
        db.close()


def test_exhausted_slow_failure_notifies_admins_and_tech_ops(db_session_factory):
    telegram = FakeTelegram()
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        job.attempt_count = 2
        db.commit()

        result = ExtractionWorker(db_session_factory).run_once(
            extractor_func=_failing(ExportTimeoutError("lento")),
            on_failure_callback=_callback(db_session_factory, telegram),
        )

        assert result["status"] == "FAILED"
        assert telegram.chats() == ADMIN_CHATS | {TECH_CHAT}
        admin_text = telegram.texts_for(1001)[0]
        assert "requiere revisión manual" in admin_text
        assert "Torres_Store *Tech*" in admin_text and "3 intentos" in admin_text
        assert "EXPORT_TIMEOUT" in telegram.texts_for(TECH_CHAT)[0]  # aviso habitual de la Story 1.4
    finally:
        db.close()


def test_hard_failure_notifies_tech_ops_only(db_session_factory):
    telegram = FakeTelegram()
    db = db_session_factory()
    try:
        ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        ExtractionWorker(db_session_factory).run_once(
            extractor_func=_failing(AuthFailedError("Credenciales revocadas")),
            on_failure_callback=_callback(db_session_factory, telegram),
        )
        assert telegram.chats() == {TECH_CHAT}
    finally:
        db.close()


def test_no_admin_with_chat_does_not_break_the_failure(db_session_factory):
    telegram = FakeTelegram()
    db = db_session_factory()
    try:
        db.query(User).filter(User.role == "ADMIN").update({"telegram_chat_id": None})
        db.commit()
        job = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)

        result = ExtractionWorker(db_session_factory).run_once(
            extractor_func=_failing(ExportTimeoutError("lento")),
            on_failure_callback=_callback(db_session_factory, telegram),
        )

        assert result["status"] == "RETRY_SCHEDULED"
        assert telegram.calls == []
        db.refresh(job)
        assert job.status == "ENQUEUED" and _within(job.next_run_at, SLOW_SECONDS)
    finally:
        db.close()


@pytest.mark.parametrize("telegram", [FakeTelegram(fail_with=RuntimeError("red caída")), FakeTelegram(result={"ok": False})])
def test_telegram_failure_still_schedules_the_retry(db_session_factory, telegram):
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        result = ExtractionWorker(db_session_factory).run_once(
            extractor_func=_failing(ExportTimeoutError("lento")),
            on_failure_callback=_callback(db_session_factory, telegram),
        )

        assert result["status"] == "RETRY_SCHEDULED"
        db.refresh(job)
        assert job.status == "ENQUEUED" and _within(job.next_run_at, SLOW_SECONDS)
        assert len(telegram.calls) == 2  # se intentó con cada administradora
    finally:
        db.close()


def test_telegram_failure_on_the_exhausted_alert_still_leaves_the_job_failed(db_session_factory):
    telegram = FakeTelegram(fail_with=RuntimeError("red caída"))
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        job.attempt_count = 2
        db.commit()

        result = ExtractionWorker(db_session_factory).run_once(
            extractor_func=_failing(ExportTimeoutError("lento")),
            on_failure_callback=_callback(db_session_factory, telegram),
        )

        assert result["status"] == "FAILED"
        db.refresh(job)
        assert job.status == "FAILED" and job.attempt_count == 3
    finally:
        db.close()


def test_notify_admins_reports_delivery(db_session_factory):
    db = db_session_factory()
    try:
        bot = TechOpsAlertBot(bot_token="123:TEST", http_dispatcher=FakeTelegram())
        assert notify_admins(db, "hola", bot=bot) == {"sent": True, "recipients_count": 2, "delivered_count": 2}
    finally:
        db.close()


# --- Trabajos atascados (AC #5) --------------------------------------------------------------


def test_stale_processing_job_is_recovered_as_slow_failure(db_session_factory):
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        other = ExtractionQueueManager.enqueue_job("biz-b", "2026-03", db)
        _stale(db, job.id)
        assert ExtractionQueueManager.get_next_runnable_job(db) is None  # la cola está bloqueada

        recovered = ExtractionQueueManager.recover_stale_jobs(db)

        assert [j.id for j in recovered] == [job.id]
        db.refresh(job)
        assert job.status == "ENQUEUED" and job.attempt_count == 1
        assert job.error_code == STALE_PROCESSING_CODE
        assert _within(job.next_run_at, SLOW_SECONDS)
        assert ExtractionQueueManager.get_next_runnable_job(db).id == other.id  # la cola avanza
    finally:
        db.close()


def test_recent_processing_job_is_left_alone(db_session_factory):
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        _stale(db, job.id, seconds=60)

        assert ExtractionQueueManager.recover_stale_jobs(db) == []
        db.refresh(job)
        assert job.status == "PROCESSING" and job.attempt_count == 0
    finally:
        db.close()


def test_stale_threshold_is_configurable_and_missing_started_at_counts_as_stale(db_session_factory):
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        _stale(db, job.id, seconds=120)

        assert ExtractionQueueManager.recover_stale_jobs(db, stale_seconds=600) == []
        assert len(ExtractionQueueManager.recover_stale_jobs(db, stale_seconds=60)) == 1

        ExtractionQueueManager.mark_job_processing(job.id, db)
        db.query(DIANExtractionJob).filter(DIANExtractionJob.id == job.id).update({"started_at": None})
        db.commit()
        assert len(ExtractionQueueManager.recover_stale_jobs(db)) == 1
    finally:
        db.close()


def test_third_stale_recovery_marks_failed_and_notifies_both_roles(db_session_factory):
    telegram = FakeTelegram()
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        job.attempt_count = 2
        db.commit()
        _stale(db, job.id)

        ExtractionWorker(db_session_factory).run_once(on_failure_callback=_callback(db_session_factory, telegram))

        db.refresh(job)
        assert job.status == "FAILED"
        assert telegram.chats() == ADMIN_CHATS | {TECH_CHAT}
        assert "requiere revisión manual" in telegram.texts_for(1001)[0]
    finally:
        db.close()


def test_local_worker_recovers_stale_job_before_taking_work(db_session_factory):
    telegram = FakeTelegram()
    db = db_session_factory()
    try:
        stale = ExtractionQueueManager.enqueue_job("biz-a", "2026-03", db)
        ExtractionQueueManager.enqueue_job("biz-b", "2026-03", db)
        _stale(db, stale.id)

        result = ExtractionWorker(db_session_factory).run_once(
            extractor_func=lambda business, period: "fake.zip",
            parser_func=lambda zip_path, business_id, session, job_id: {"ok": True},
            on_failure_callback=_callback(db_session_factory, telegram),
        )

        assert result["status"] == "SUCCESS"  # el otro trabajo se ejecutó
        db.refresh(stale)
        assert stale.status == "ENQUEUED" and stale.error_code == STALE_PROCESSING_CODE
        assert telegram.chats() == ADMIN_CHATS
        assert "no respondió a tiempo" in telegram.texts_for(1001)[0]
    finally:
        db.close()
