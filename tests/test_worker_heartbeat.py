"""Pruebas de la Story 1.8 (AC #1 y #2): latido del worker y vigilancia de su silencio."""

import importlib.util
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import Base, Business, DIANExtractionJob, User, WorkerHeartbeat
from dian_automation.queue.scheduler import ExtractionScheduler
from dian_automation.queue.worker_heartbeat import (
    ALERT_INTERVAL,
    WorkerSilenceMonitor,
    normalize_worker_name,
    record_heartbeat,
)
from dian_automation.telegram.tech_ops_bot import TechOpsAlertBot

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 20, 15, 0, 0)  # naive = UTC; 10:00 en Bogotá


class FakeTelegram:
    """Sustituye el envío real: guarda cada mensaje y responde ok (o no) según `ok`."""

    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def __call__(self, endpoint, data, files):
        self.sent.append((endpoint, data))
        return {"ok": self.ok}

    def chats(self):
        return sorted(d["chat_id"] for _, d in self.sent)


@pytest.fixture
def factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def telegram():
    return FakeTelegram()


@pytest.fixture
def monitor(factory, telegram):
    add_recipients(factory)
    return make_monitor(factory, telegram)


def make_monitor(factory, telegram, **kwargs):
    bot = TechOpsAlertBot(bot_token="123:TEST", http_dispatcher=telegram)
    kwargs.setdefault("silence_minutes", 15)
    kwargs.setdefault("stale_seconds", 900)
    return WorkerSilenceMonitor(factory, bot=bot, **kwargs)


def add_business(factory):
    db = factory()
    try:
        if not db.get(User, "usr-cli"):
            db.add(User(id="usr-cli", email="cli@test.com", full_name="Cliente", role="CLIENT", telegram_chat_id=3003))
        if not db.get(Business, "biz-1"):
            db.add(
                Business(
                    id="biz-1", client_id="usr-cli", legal_name="Empresa Uno SAS", commercial_name="Empresa Uno",
                    nit="900111222", dv="1", taxpayer_type="PERSONA_JURIDICA",
                )
            )
        db.commit()
    finally:
        db.close()


def add_recipients(factory):
    add_business(factory)
    db = factory()
    try:
        db.add(User(id="usr-ti", email="ti@test.com", full_name="Soporte TI", role="TECH_OPS", telegram_chat_id=1001))
        db.add(User(id="usr-admin", email="admin@test.com", full_name="Administradora", role="ADMIN", telegram_chat_id=2002))
        db.commit()
    finally:
        db.close()


def add_job(factory, status="ENQUEUED", next_run_at=None, started_at=None, key="a"):
    db = factory()
    try:
        db.add(
            DIANExtractionJob(
                id=f"job-{key}", business_id="biz-1", target_period="2026-09", status=status,
                next_run_at=next_run_at or NOW - timedelta(minutes=30), started_at=started_at,
            )
        )
        db.commit()
    finally:
        db.close()


def set_heartbeat(factory, name="remote", seen=None, alerted=None):
    db = factory()
    try:
        db.add(WorkerHeartbeat(name=name, last_seen_at=seen, last_alert_at=alerted))
        db.commit()
    finally:
        db.close()


def heartbeats(factory):
    db = factory()
    try:
        return {w.name: (w.last_seen_at, w.last_alert_at) for w in db.query(WorkerHeartbeat).all()}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# AC #1: latido
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [(None, "remote"), ("", "remote"), ("   ", "remote"), (" casa ", "casa"), ("x" * 300, "x" * 100)],
)
def test_worker_name_is_trimmed_and_defaults_to_remote(raw, expected):
    assert normalize_worker_name(raw) == expected


def test_record_heartbeat_creates_the_row_and_then_updates_it(factory):
    db = factory()
    try:
        first = record_heartbeat(db, "remote", now=NOW)
        assert (first.name, first.last_seen_at, first.last_alert_at) == ("remote", NOW, None)
        first.last_alert_at = NOW
        db.commit()

        record_heartbeat(db, "remote", now=NOW + timedelta(minutes=1))

        assert db.query(WorkerHeartbeat).count() == 1
    finally:
        db.close()
    assert heartbeats(factory) == {"remote": (NOW + timedelta(minutes=1), None)}


def test_record_heartbeat_reads_an_aware_clock_as_utc(factory):
    db = factory()
    try:
        row = record_heartbeat(db, "remote", now=datetime(2026, 9, 20, 10, 0, tzinfo=timezone(timedelta(hours=-5))))
        assert row.last_seen_at == NOW
    finally:
        db.close()


# ---------------------------------------------------------------------------
# AC #2: silencio
# ---------------------------------------------------------------------------

def test_silence_with_a_runnable_job_alerts_tech_ops_and_admin_once(factory, monitor, telegram):
    set_heartbeat(factory, seen=NOW - timedelta(minutes=16))
    add_job(factory)

    result = monitor.check(NOW)

    assert result["alerted"] is True and result["pending_jobs"] == 1
    assert telegram.chats() == [1001, 2002]  # nunca al cliente
    assert heartbeats(factory)["remote"][1] == NOW


def test_the_alert_says_when_it_last_answered_how_long_and_how_many_jobs(factory, monitor, telegram):
    set_heartbeat(factory, seen=NOW - timedelta(hours=2, minutes=15))
    add_job(factory, key="a")
    add_job(factory, key="b")

    monitor.check(NOW)

    endpoint, data = telegram.sent[0]
    assert endpoint == "sendMessage" and "parse_mode" not in data  # texto plano
    text = data["text"]
    assert "no responde" in text
    assert "20/09/2026 07:45" in text and "hora de Bogotá" in text  # 12:45 UTC = 07:45 Bogotá
    assert "hace 2 h 15 min" in text
    assert "Trabajos pendientes: 2" in text


def test_the_alert_is_not_repeated_within_an_hour_but_is_after(factory, monitor, telegram):
    set_heartbeat(factory, seen=NOW - timedelta(minutes=20))
    add_job(factory)
    monitor.check(NOW)

    same_hour = monitor.check(NOW + ALERT_INTERVAL - timedelta(seconds=1))
    next_hour = monitor.check(NOW + ALERT_INTERVAL + timedelta(seconds=1))

    assert same_hour == {"alerted": False, "reason": "ALREADY_ALERTED"}
    assert next_hour["alerted"] is True
    assert len(telegram.sent) == 4  # dos avisos a dos destinatarios


def test_a_heartbeat_after_the_alert_rearms_it_for_the_next_silence(factory, monitor, telegram):
    set_heartbeat(factory, seen=NOW - timedelta(minutes=20))
    add_job(factory)
    monitor.check(NOW)
    db = factory()
    try:
        record_heartbeat(db, "remote", now=NOW + timedelta(minutes=5))
    finally:
        db.close()

    later = monitor.check(NOW + timedelta(minutes=25))  # 20 min de silencio nuevo, a solo 25 min del primer aviso

    assert later["alerted"] is True


@pytest.mark.parametrize("state", ["empty_queue", "future_job", "success_job"])
def test_silence_without_a_runnable_job_does_not_alert(factory, monitor, telegram, state):
    set_heartbeat(factory, seen=NOW - timedelta(hours=5))
    if state == "future_job":
        add_job(factory, next_run_at=NOW + timedelta(hours=6))
    if state == "success_job":
        add_job(factory, status="SUCCESS")

    assert monitor.check(NOW) == {"alerted": False, "reason": "NO_PENDING_JOBS"}
    assert telegram.sent == []


def test_a_recent_heartbeat_does_not_alert_even_with_jobs_waiting(factory, monitor, telegram):
    set_heartbeat(factory, seen=NOW - timedelta(minutes=10))
    add_job(factory)

    assert monitor.check(NOW) == {"alerted": False, "reason": "WORKER_ALIVE"}
    assert telegram.sent == []


def test_exactly_at_the_limit_is_not_yet_silence(factory, monitor):
    set_heartbeat(factory, seen=NOW - timedelta(minutes=15))
    add_job(factory)

    assert monitor.check(NOW)["reason"] == "WORKER_ALIVE"


def test_a_worker_busy_with_a_recent_processing_job_is_not_silent(factory, monitor, telegram):
    set_heartbeat(factory, seen=NOW - timedelta(minutes=40))
    add_job(factory, status="PROCESSING", started_at=NOW - timedelta(minutes=10), key="run")
    add_job(factory, key="wait")

    assert monitor.check(NOW) == {"alerted": False, "reason": "WORKER_BUSY"}
    assert telegram.sent == []


def test_a_stuck_processing_job_counts_as_pending_work(factory, monitor, telegram):
    set_heartbeat(factory, seen=NOW - timedelta(minutes=45))
    add_job(factory, status="PROCESSING", started_at=NOW - timedelta(minutes=44))  # más de STALE (15 min)

    result = monitor.check(NOW)

    assert result["alerted"] is True and result["pending_jobs"] == 1


def test_a_worker_that_never_reported_is_measured_from_its_oldest_runnable_job(factory, monitor, telegram):
    add_job(factory, next_run_at=NOW - timedelta(minutes=20))

    result = monitor.check(NOW)

    assert result["alerted"] is True
    assert "Nunca ha reportado" in telegram.sent[0][1]["text"]
    assert heartbeats(factory) == {"remote": (None, NOW)}
    assert monitor.check(NOW + timedelta(minutes=1)) == {"alerted": False, "reason": "ALREADY_ALERTED"}


def test_a_worker_that_never_reported_is_given_the_grace_time_from_the_job(factory, monitor, telegram):
    add_job(factory, next_run_at=NOW - timedelta(minutes=5))

    assert monitor.check(NOW)["reason"] == "WORKER_ALIVE"
    assert heartbeats(factory) == {}


def test_the_first_heartbeat_fills_the_placeholder_row(factory, monitor):
    add_job(factory, next_run_at=NOW - timedelta(minutes=20))
    monitor.check(NOW)
    db = factory()
    try:
        record_heartbeat(db, "remote", now=NOW + timedelta(minutes=2))
    finally:
        db.close()

    assert heartbeats(factory) == {"remote": (NOW + timedelta(minutes=2), None)}


def test_any_live_worker_is_enough(factory, monitor, telegram):
    set_heartbeat(factory, name="casa", seen=NOW - timedelta(hours=8))
    set_heartbeat(factory, name="raspberry", seen=NOW - timedelta(minutes=2))
    add_job(factory)

    assert monitor.check(NOW)["reason"] == "WORKER_ALIVE"
    assert telegram.sent == []


def test_the_alert_mark_is_kept_on_the_most_recent_worker(factory, monitor):
    set_heartbeat(factory, name="casa", seen=NOW - timedelta(hours=8))
    set_heartbeat(factory, name="raspberry", seen=NOW - timedelta(minutes=30))
    add_job(factory)

    monitor.check(NOW)

    marks = heartbeats(factory)
    assert marks["raspberry"][1] == NOW and marks["casa"][1] is None


def test_without_recipients_nothing_is_marked_and_it_is_retried_next_cycle(factory, telegram):
    add_business(factory)
    set_heartbeat(factory, seen=NOW - timedelta(minutes=20))
    add_job(factory)
    monitor = make_monitor(factory, telegram)

    first = monitor.check(NOW)
    assert first["alerted"] is False and heartbeats(factory)["remote"][1] is None

    add_recipients(factory)
    second = monitor.check(NOW + timedelta(minutes=1))

    assert second["alerted"] is True and len(telegram.sent) == 2


def test_a_telegram_rejection_is_not_marked_as_sent(factory):
    add_recipients(factory)
    set_heartbeat(factory, seen=NOW - timedelta(minutes=20))
    add_job(factory)
    monitor = make_monitor(factory, FakeTelegram(ok=False))

    assert monitor.check(NOW)["alerted"] is False
    assert heartbeats(factory)["remote"][1] is None


def test_one_recipient_group_is_enough_to_count_as_delivered(factory):
    db = factory()
    db.add(User(id="usr-admin", email="admin@test.com", full_name="Administradora", role="ADMIN", telegram_chat_id=2002))
    db.add(User(id="usr-cli", email="cli@test.com", full_name="Cliente", role="CLIENT"))
    db.add(
        Business(
            id="biz-1", client_id="usr-cli", legal_name="Empresa SAS", commercial_name="Empresa",
            nit="900111222", dv="1", taxpayer_type="PERSONA_JURIDICA",
        )
    )
    db.commit()
    db.close()
    telegram = FakeTelegram()
    set_heartbeat(factory, seen=NOW - timedelta(minutes=20))
    add_job(factory)

    assert make_monitor(factory, telegram).check(NOW)["alerted"] is True
    assert telegram.chats() == [2002]


@pytest.mark.parametrize("minutes", [0, -1])
def test_invalid_silence_minutes_are_rejected(factory, minutes):
    with pytest.raises(ValueError):
        WorkerSilenceMonitor(factory, silence_minutes=minutes)


def test_the_monitor_reads_an_aware_clock_as_utc(factory, monitor, telegram):
    set_heartbeat(factory, seen=NOW - timedelta(minutes=20))
    add_job(factory)

    assert monitor.check(datetime(2026, 9, 20, 10, 0, tzinfo=timezone(timedelta(hours=-5))))["alerted"] is True


# ---------------------------------------------------------------------------
# El ciclo del scheduler corre la vigilancia aunque esté deshabilitado
# ---------------------------------------------------------------------------

def test_the_watch_runs_every_cycle_even_when_scheduling_is_disabled(factory):
    calls = []
    scheduler = ExtractionScheduler(factory, worker_watch=lambda now: calls.append(now))

    scheduler.run_loop(enabled=False, sleep=lambda s: None, now_func=lambda: NOW, max_iterations=3)

    assert calls == [NOW, NOW, NOW]


def test_the_watch_also_runs_when_scheduling_is_enabled(factory):
    calls = []
    scheduler = ExtractionScheduler(factory, worker_watch=lambda now: calls.append(now))

    scheduler.run_loop(enabled=True, sleep=lambda s: None, now_func=lambda: NOW, max_iterations=2)

    assert calls == [NOW, NOW]


def test_disabled_scheduler_alerts_about_a_silent_worker_without_enqueuing(factory, monitor, telegram):
    set_heartbeat(factory, seen=NOW - timedelta(hours=1))
    add_job(factory)
    scheduler = ExtractionScheduler(factory, worker_watch=monitor.check)

    scheduler.run_loop(enabled=False, sleep=lambda s: None, now_func=lambda: NOW, max_iterations=2)

    assert telegram.chats() == [1001, 2002]  # un solo aviso: el segundo ciclo cae dentro de la hora
    db = factory()
    try:
        assert db.query(DIANExtractionJob).count() == 1
    finally:
        db.close()


def test_an_error_in_the_watch_does_not_stop_the_loop(factory, caplog):
    calls = []

    def flaky(now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("base no disponible")

    scheduler = ExtractionScheduler(factory, worker_watch=flaky)

    with caplog.at_level(logging.ERROR):
        scheduler.run_loop(enabled=False, sleep=lambda s: None, now_func=lambda: NOW, max_iterations=3)

    assert len(calls) == 3
    assert "vigilancia del worker" in caplog.text


def test_a_scheduler_without_watch_behaves_as_before(factory):
    scheduler = ExtractionScheduler(factory)

    scheduler.run_loop(enabled=False, sleep=lambda s: None, max_iterations=2)

    assert scheduler.worker_watch is None


# ---------------------------------------------------------------------------
# Runner y configuración
# ---------------------------------------------------------------------------

def test_the_runner_wires_the_monitor_into_the_scheduler(monkeypatch):
    import run_scheduler

    monkeypatch.setattr(
        run_scheduler, "config",
        SimpleNamespace(scheduler_enabled=False, scheduler_weekday=6, scheduler_hour=3, worker_silence_minutes=7),
    )
    seen = []
    monkeypatch.setattr(ExtractionScheduler, "run_loop", lambda self, enabled, interval: seen.append(self.worker_watch))

    run_scheduler.main()

    (watch,) = seen
    assert isinstance(watch.__self__, WorkerSilenceMonitor)
    assert watch.__self__.silence == timedelta(minutes=7)


def test_the_runner_exits_with_an_error_on_invalid_silence_minutes(monkeypatch, caplog):
    import run_scheduler

    monkeypatch.setattr(
        run_scheduler, "config",
        SimpleNamespace(scheduler_enabled=False, scheduler_weekday=6, scheduler_hour=3, worker_silence_minutes=0),
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exit_info:
            run_scheduler.main()

    assert exit_info.value.code == 1
    assert "WORKER_SILENCE_MINUTES" in caplog.text


def _load_config(monkeypatch, **env):
    monkeypatch.delenv("WORKER_SILENCE_MINUTES", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    spec = importlib.util.spec_from_file_location("config_under_test", PROJECT_ROOT / "src" / "dian_automation" / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.config


def test_config_silence_minutes_defaults_to_15_and_is_configurable(monkeypatch):
    assert _load_config(monkeypatch).worker_silence_minutes == 15
    assert _load_config(monkeypatch, WORKER_SILENCE_MINUTES="").worker_silence_minutes == 15
    assert _load_config(monkeypatch, WORKER_SILENCE_MINUTES="30").worker_silence_minutes == 30


def test_env_example_documents_the_new_variables():
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    environment_doc = (PROJECT_ROOT / "docs" / "ENVIRONMENT.md").read_text(encoding="utf-8")

    for name in ("WORKER_SILENCE_MINUTES", "WORKER_NAME", "PENDING_UPLOADS_DIR"):
        assert f"{name}=" in env_example
        assert f"`{name}`" in environment_doc
