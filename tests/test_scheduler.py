"""Pruebas de la Story 1.7: programador semanal de descarga del mes en curso y cierre del mes anterior."""

import importlib.util
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import dian_automation.queue.scheduler as scheduler_module
from dian_automation.db.models import Base, Business, DIANExtractionJob, Subscription, User
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.queue.scheduler import BOGOTA_TZ, ExtractionScheduler

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SUNDAY = (2026, 9, 20)  # domingo, día 20: solo mes en curso
MONDAY = (2026, 9, 21)
THURSDAY_DAY_3 = (2026, 9, 3)  # jueves día 3: solo cierre del mes anterior
SUNDAY_DAY_4 = (2026, 10, 4)  # domingo día 4: cierre y mes en curso


def at(year, month, day, hour=3, minute=0):
    """Instante con hora de pared de Bogotá (UTC-5, sin horario de verano)."""
    return datetime(year, month, day, hour, minute, tzinfo=BOGOTA_TZ)


def utc_naive(local):
    return local.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def scheduler(factory):
    return ExtractionScheduler(factory, weekday=6, hour=3)


def add_business(factory, key, income_source="DIAN", is_active=True, subscription_status=None, created_at=None):
    """Crea un cliente con un negocio (id `biz-<key>`) y, opcionalmente, una suscripción."""
    db = factory()
    try:
        client = User(id=f"usr-{key}", email=f"{key}@test.com", full_name=f"Cliente {key}", role="CLIENT")
        db.add(client)
        db.add(
            Business(
                id=f"biz-{key}", client_id=client.id, legal_name=f"Negocio {key} SAS", commercial_name=f"Negocio {key}",
                nit=f"9000{abs(hash(key)) % 10000:04d}", dv="1", taxpayer_type="PERSONA_JURIDICA",
                income_source=income_source, is_active=is_active,
                created_at=created_at or datetime(2026, 1, 1),
            )
        )
        if subscription_status:
            db.add(
                Subscription(
                    client_id=client.id, plan="TRIMESTRAL", discount_rate=5, base_price=100000, final_price=95000,
                    start_date=date(2026, 1, 1), cutoff_date=date(2026, 4, 1), grace_period_end=date(2026, 4, 4),
                    status=subscription_status,
                )
            )
        db.commit()
    finally:
        db.close()


def add_job(factory, key, period, status="SUCCESS", created_at=None, finished_at=None):
    db = factory()
    try:
        db.add(
            DIANExtractionJob(
                business_id=f"biz-{key}", target_period=period, status=status,
                created_at=created_at or datetime(2026, 1, 1),
                finished_at=finished_at if finished_at is not None else (created_at or datetime(2026, 1, 1)),
            )
        )
        db.commit()
    finally:
        db.close()


def jobs(factory, **filters):
    db = factory()
    try:
        return db.query(DIANExtractionJob).filter_by(**filters).order_by(DIANExtractionJob.created_at).all()
    finally:
        db.close()


def periods_by_business(factory):
    result = {}
    for job in jobs(factory, status="ENQUEUED"):
        result.setdefault(job.business_id, []).append(job.target_period)
    return result


# ---------------------------------------------------------------------------
# AC #1: domingo 03:00, mes en curso, ambos tipos de cliente
# ---------------------------------------------------------------------------

def test_sunday_at_3am_enqueues_the_current_month_for_every_active_business(factory, scheduler):
    add_business(factory, "dian", income_source="DIAN")
    add_business(factory, "manual", income_source="MANUAL_SALES")

    result = scheduler.tick(at(*SUNDAY))

    assert result["ran"] is True
    assert periods_by_business(factory) == {"biz-dian": ["2026-09"], "biz-manual": ["2026-09"]}
    assert len(result["enqueued"]) == 2


@pytest.mark.parametrize(
    "when",
    [
        at(*MONDAY),  # lunes 03:00: no es el día
        at(*SUNDAY, hour=4),  # domingo 04:00: fuera de la ventana
        at(*SUNDAY, hour=2, minute=59),
        at(2026, 9, 19),  # sábado 03:00
    ],
)
def test_outside_the_weekly_window_nothing_is_enqueued(factory, scheduler, when):
    add_business(factory, "a")

    result = scheduler.tick(when)

    assert result["ran"] is False
    assert result["enqueued"] == []
    assert jobs(factory) == []


def test_the_whole_hour_of_the_window_counts(factory, scheduler):
    add_business(factory, "a")

    assert scheduler.tick(at(*SUNDAY, hour=3, minute=59))["ran"] is True
    assert periods_by_business(factory) == {"biz-a": ["2026-09"]}


def test_a_naive_clock_is_read_as_utc(factory, scheduler):
    add_business(factory, "a")

    # 08:00 UTC del domingo = 03:00 en Bogotá
    assert scheduler.tick(datetime(2026, 9, 20, 8, 0))["ran"] is True
    assert scheduler.tick(datetime(2026, 9, 20, 3, 0))["ran"] is False  # 22:00 del sábado en Bogotá


def test_weekday_and_hour_are_configurable(factory):
    add_business(factory, "a")
    monday_at_5 = ExtractionScheduler(factory, weekday=0, hour=5)

    assert monday_at_5.tick(at(*MONDAY, hour=5))["ran"] is True
    assert periods_by_business(factory) == {"biz-a": ["2026-09"]}


# ---------------------------------------------------------------------------
# AC #2: cierre del mes anterior los días 1 a 5
# ---------------------------------------------------------------------------

def test_days_1_to_5_enqueue_only_the_closing_of_the_previous_month_when_it_is_not_the_weekly_day(factory, scheduler):
    add_business(factory, "a")

    result = scheduler.tick(at(*THURSDAY_DAY_3))

    assert result["periods"] == ["2026-08"]
    assert periods_by_business(factory) == {"biz-a": ["2026-08"]}


def test_the_weekly_day_inside_days_1_to_5_enqueues_both_periods(factory, scheduler):
    add_business(factory, "a")

    result = scheduler.tick(at(*SUNDAY_DAY_4))

    assert result["periods"] == ["2026-09", "2026-10"]
    assert sorted(periods_by_business(factory)["biz-a"]) == ["2026-09", "2026-10"]


def test_day_6_is_no_longer_the_closing_window(factory, scheduler):
    add_business(factory, "a")

    assert scheduler.tick(at(2026, 10, 6))["ran"] is False  # martes 6: ya no es cierre ni domingo


def test_closing_skips_a_business_whose_previous_month_succeeded_after_day_1(factory, scheduler):
    add_business(factory, "done")
    add_business(factory, "stale")
    add_business(factory, "never")
    # 2026-08 descargado el 2 de septiembre (posterior al cierre): no se repite
    add_job(factory, "done", "2026-08", finished_at=utc_naive(at(2026, 9, 2, hour=10)))
    # 2026-08 descargado el 25 de agosto (antes del cierre): falta el cierre
    add_job(factory, "stale", "2026-08", finished_at=utc_naive(at(2026, 8, 25, hour=10)))

    scheduler.tick(at(*THURSDAY_DAY_3))

    assert periods_by_business(factory) == {"biz-stale": ["2026-08"], "biz-never": ["2026-08"]}


def test_closing_a_january_run_targets_december_of_the_previous_year(factory, scheduler):
    add_business(factory, "a")

    result = scheduler.tick(at(2027, 1, 1))  # viernes 1 de enero: solo cierre

    assert result["periods"] == ["2026-12"]


def test_success_finished_just_after_midnight_of_day_1_in_bogota_counts_as_closing(factory, scheduler):
    add_business(factory, "a")
    add_job(factory, "a", "2026-08", finished_at=utc_naive(at(2026, 9, 1, hour=0, minute=1)))

    assert scheduler.tick(at(*THURSDAY_DAY_3))["enqueued"] == []


def test_success_finished_just_before_midnight_of_day_1_in_bogota_does_not_count(factory, scheduler):
    add_business(factory, "a")
    add_job(factory, "a", "2026-08", finished_at=utc_naive(at(2026, 8, 31, hour=23, minute=59)))

    assert len(scheduler.tick(at(*THURSDAY_DAY_3))["enqueued"]) == 1


# ---------------------------------------------------------------------------
# AC #3: idempotencia
# ---------------------------------------------------------------------------

def test_a_second_cycle_in_the_same_window_creates_no_duplicates(factory, scheduler):
    add_business(factory, "a")
    add_business(factory, "b")

    first = scheduler.tick(at(*SUNDAY))
    second = scheduler.tick(at(*SUNDAY, minute=1))

    assert len(first["enqueued"]) == 2
    assert second["enqueued"] == []
    assert len(jobs(factory)) == 2


def test_two_scheduler_instances_do_not_duplicate_jobs(factory):
    add_business(factory, "a")

    ExtractionScheduler(factory, weekday=6, hour=3).tick(at(*SUNDAY))
    ExtractionScheduler(factory, weekday=6, hour=3).tick(at(*SUNDAY, minute=1))

    assert len(jobs(factory)) == 1


def test_the_60_second_loop_does_not_requeue_once_the_first_job_has_succeeded(factory, scheduler):
    add_business(factory, "a")
    add_business(factory, "b")
    scheduler.tick(at(*SUNDAY))

    db = factory()
    try:
        first = ExtractionQueueManager.get_next_runnable_job(db)
        ExtractionQueueManager.mark_job_processing(first.id, db)
        ExtractionQueueManager.mark_job_success(first.id, "a.zip", db)
    finally:
        db.close()

    later = scheduler.tick(at(*SUNDAY, minute=30))

    assert later["enqueued"] == []
    assert len(jobs(factory)) == 2


@pytest.mark.parametrize("status", ["ENQUEUED", "PROCESSING"])
def test_a_pending_job_of_the_same_period_from_before_the_window_blocks_a_new_one(factory, scheduler, status):
    add_business(factory, "a")
    add_job(factory, "a", "2026-09", status=status, created_at=utc_naive(at(2026, 9, 18)))

    assert scheduler.tick(at(*SUNDAY))["enqueued"] == []


def test_a_pending_job_of_another_period_does_not_block(factory, scheduler):
    add_business(factory, "a")
    add_job(factory, "a", "2026-08", status="ENQUEUED", created_at=utc_naive(at(2026, 9, 18)))

    assert len(scheduler.tick(at(*SUNDAY))["enqueued"]) == 1


def test_last_weeks_finished_jobs_do_not_block_this_weeks_run(factory, scheduler):
    add_business(factory, "ok")
    add_business(factory, "failed")
    add_job(factory, "ok", "2026-09", status="SUCCESS", created_at=utc_naive(at(2026, 9, 13)), finished_at=utc_naive(at(2026, 9, 13, hour=4)))
    add_job(factory, "failed", "2026-09", status="FAILED", created_at=utc_naive(at(2026, 9, 13)))

    scheduler.tick(at(*SUNDAY))

    assert periods_by_business(factory) == {"biz-ok": ["2026-09"], "biz-failed": ["2026-09"]}


def test_a_job_created_manually_earlier_in_the_window_blocks_a_duplicate(factory, scheduler):
    add_business(factory, "a")
    add_job(factory, "a", "2026-09", status="SUCCESS", created_at=utc_naive(at(*SUNDAY, minute=10)), finished_at=utc_naive(at(*SUNDAY, minute=20)))

    assert scheduler.tick(at(*SUNDAY, minute=30))["enqueued"] == []


# ---------------------------------------------------------------------------
# AC #4: negocios omitidos
# ---------------------------------------------------------------------------

def test_inactive_businesses_and_blocked_subscriptions_are_skipped(factory, scheduler):
    add_business(factory, "ok", subscription_status="ACTIVO")
    add_business(factory, "in-arrears", subscription_status="EN_MORA")
    add_business(factory, "no-subscription")
    add_business(factory, "inactive", is_active=False)
    add_business(factory, "blocked", subscription_status="BLOQUEADO")

    scheduler.tick(at(*SUNDAY))

    assert set(periods_by_business(factory)) == {"biz-ok", "biz-in-arrears", "biz-no-subscription"}


# ---------------------------------------------------------------------------
# AC #5: orden, escalonado con varios clientes y solo encola
# ---------------------------------------------------------------------------

def test_businesses_are_enqueued_from_the_oldest_successful_extraction_to_the_newest(factory, scheduler):
    add_business(factory, "recent")
    add_business(factory, "oldest")
    add_business(factory, "never-b", created_at=datetime(2026, 3, 1))
    add_business(factory, "never-a", created_at=datetime(2026, 2, 1))
    add_business(factory, "middle")
    add_job(factory, "recent", "2026-08", finished_at=datetime(2026, 9, 14))
    add_job(factory, "oldest", "2026-06", finished_at=datetime(2026, 7, 1))
    add_job(factory, "middle", "2026-08", finished_at=datetime(2026, 8, 20))
    # un fallo reciente no cuenta como extracción exitosa
    add_job(factory, "oldest", "2026-09", status="FAILED", created_at=datetime(2026, 9, 15), finished_at=datetime(2026, 9, 15))

    result = scheduler.tick(at(*SUNDAY))

    assert [e["business_id"] for e in result["enqueued"]] == [
        "biz-never-a", "biz-never-b", "biz-oldest", "biz-middle", "biz-recent",
    ]


def test_the_closing_of_the_previous_month_is_enqueued_before_the_current_month(factory, scheduler):
    add_business(factory, "a")

    result = scheduler.tick(at(*SUNDAY_DAY_4))

    assert [e["period"] for e in result["enqueued"]] == ["2026-09", "2026-10"]


def _drain(factory, pacing_seconds=0):
    """Procesa la cola como lo haría el worker (uno a la vez) y devuelve el orden de entrega."""
    db = factory()
    order = []
    try:
        while True:
            job = ExtractionQueueManager.get_next_runnable_job(db)
            if not job:
                return order
            ExtractionQueueManager.mark_job_processing(job.id, db)
            assert ExtractionQueueManager.get_next_runnable_job(db) is None  # concurrencia 1
            ExtractionQueueManager.mark_job_success(job.id, "x.zip", db, pacing_seconds=pacing_seconds)
            order.append(job.business_id)
    finally:
        db.close()


def test_with_eight_clients_the_queue_hands_them_out_one_at_a_time_in_enqueue_order(factory, scheduler):
    for i in range(8):
        add_business(factory, f"c{i}", income_source="DIAN" if i % 2 else "MANUAL_SALES")
    # antigüedad de la última extracción exitosa: c7 la más vieja ... c0 la más reciente
    for i in range(8):
        add_job(factory, f"c{i}", "2026-08", finished_at=datetime(2026, 9, 1) + timedelta(days=i * -1 + 7))

    result = scheduler.tick(at(*SUNDAY))
    enqueued_order = [e["business_id"] for e in result["enqueued"]]

    assert len(enqueued_order) == 8
    assert enqueued_order == [f"biz-c{i}" for i in (0, 1, 2, 3, 4, 5, 6, 7)][::-1]
    assert _drain(factory) == enqueued_order


def test_with_eight_clients_each_one_waits_15_minutes_after_the_previous_success(factory, scheduler):
    for i in range(8):
        add_business(factory, f"c{i}")
    scheduler.tick(at(*SUNDAY))

    db = factory()
    try:
        first = ExtractionQueueManager.get_next_runnable_job(db)
        ExtractionQueueManager.mark_job_processing(first.id, db)
        ExtractionQueueManager.mark_job_success(first.id, "x.zip", db)  # espaciado por defecto: 15 min

        assert ExtractionQueueManager.get_next_runnable_job(db) is None
        waiting = db.query(DIANExtractionJob).filter(DIANExtractionJob.status == "ENQUEUED").all()
        assert len(waiting) == 7
        assert all(j.next_run_at >= datetime.utcnow() + timedelta(minutes=14) for j in waiting)
    finally:
        db.close()


def test_the_scheduler_only_enqueues_and_never_runs_extractions():
    source = Path(scheduler_module.__file__).read_text(encoding="utf-8")

    assert "dian_flow" not in source
    assert "mark_job_" not in source
    assert "enqueue_job" in source


# ---------------------------------------------------------------------------
# Resiliencia del ciclo
# ---------------------------------------------------------------------------

def test_one_business_failing_to_enqueue_does_not_stop_the_others(factory, scheduler, monkeypatch):
    add_business(factory, "a", created_at=datetime(2026, 1, 1))
    add_business(factory, "b", created_at=datetime(2026, 1, 2))
    real_enqueue = ExtractionQueueManager.enqueue_job.__func__

    def flaky(cls, business_id, target_period, db, delay_seconds=0):
        if business_id == "biz-a":
            raise RuntimeError("base bloqueada")
        return real_enqueue(cls, business_id, target_period, db, delay_seconds)

    monkeypatch.setattr(ExtractionQueueManager, "enqueue_job", classmethod(flaky))

    result = scheduler.tick(at(*SUNDAY))

    assert result["errors"] == 1
    assert [e["business_id"] for e in result["enqueued"]] == ["biz-b"]


# ---------------------------------------------------------------------------
# AC #6: bandera SCHEDULER_ENABLED y bucle
# ---------------------------------------------------------------------------

def test_a_disabled_loop_starts_logs_and_enqueues_nothing(factory, scheduler, caplog):
    add_business(factory, "a")
    sleeps = []

    with caplog.at_level(logging.INFO, logger="scheduler"):
        scheduler.run_loop(enabled=False, interval=60, sleep=sleeps.append, now_func=lambda: at(*SUNDAY), max_iterations=3)

    assert "deshabilitado" in caplog.text
    assert jobs(factory) == []
    assert sleeps == [60, 60, 60]  # sigue vivo


def test_an_enabled_loop_enqueues_once_across_repeated_cycles(factory, scheduler, caplog):
    add_business(factory, "a")
    add_business(factory, "b")

    with caplog.at_level(logging.INFO, logger="scheduler"):
        scheduler.run_loop(enabled=True, sleep=lambda s: None, now_func=lambda: at(*SUNDAY), max_iterations=5)

    assert len(jobs(factory)) == 2
    assert "habilitado" in caplog.text and "deshabilitado" not in caplog.text


def test_the_loop_survives_a_failing_cycle(factory, scheduler, monkeypatch):
    add_business(factory, "a")
    real_tick = scheduler.tick
    calls = []

    def flaky_tick(now=None):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("fallo transitorio")
        return real_tick(now)

    monkeypatch.setattr(scheduler, "tick", flaky_tick)

    scheduler.run_loop(enabled=True, sleep=lambda s: None, now_func=lambda: at(*SUNDAY), max_iterations=3)

    assert len(calls) == 3
    assert len(jobs(factory)) == 1


@pytest.mark.parametrize("weekday,hour", [(7, 3), (-1, 3), (6, 24), (6, -1)])
def test_invalid_weekday_or_hour_is_rejected(factory, weekday, hour):
    with pytest.raises(ValueError):
        ExtractionScheduler(factory, weekday=weekday, hour=hour)


def test_the_runner_exits_with_an_error_on_an_invalid_schedule(monkeypatch, caplog):
    import run_scheduler
    from types import SimpleNamespace

    monkeypatch.setattr(run_scheduler, "config", SimpleNamespace(scheduler_enabled=True, scheduler_weekday=9, scheduler_hour=3))

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exit_info:
            run_scheduler.main()

    assert exit_info.value.code == 1
    assert "SCHEDULER_WEEKDAY" in caplog.text


def test_the_runner_passes_the_configured_flag_and_schedule_to_the_loop(monkeypatch):
    import run_scheduler
    from types import SimpleNamespace

    monkeypatch.setattr(run_scheduler, "config", SimpleNamespace(scheduler_enabled=False, scheduler_weekday=0, scheduler_hour=5))
    calls = []
    monkeypatch.setattr(
        ExtractionScheduler, "run_loop",
        lambda self, enabled, interval: calls.append((self.weekday, self.hour, enabled, interval)),
    )

    run_scheduler.main()

    assert calls == [(0, 5, False, 60)]


def _load_config(monkeypatch, **env):
    """Carga config.py aparte (sin tocar el `config` compartido ni el .env local) con el entorno dado."""
    for name in ("SCHEDULER_ENABLED", "SCHEDULER_WEEKDAY", "SCHEDULER_HOUR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    spec = importlib.util.spec_from_file_location("config_under_test", PROJECT_ROOT / "src" / "dian_automation" / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.config


def test_config_defaults_to_disabled_on_sundays_at_3(monkeypatch):
    cfg = _load_config(monkeypatch)

    assert cfg.scheduler_enabled is False
    assert cfg.scheduler_weekday == 6
    assert cfg.scheduler_hour == 3


@pytest.mark.parametrize("value,expected", [("true", True), ("TRUE", True), (" true ", True), ("false", False), ("1", False), ("yes", False), ("", False)])
def test_only_the_value_true_enables_the_scheduler(monkeypatch, value, expected):
    assert _load_config(monkeypatch, SCHEDULER_ENABLED=value).scheduler_enabled is expected


def test_config_reads_weekday_and_hour_from_the_environment(monkeypatch):
    cfg = _load_config(monkeypatch, SCHEDULER_WEEKDAY="0", SCHEDULER_HOUR="5")

    assert (cfg.scheduler_weekday, cfg.scheduler_hour) == (0, 5)


# ---------------------------------------------------------------------------
# AC #7: despliegue
# ---------------------------------------------------------------------------

def test_deployment_files_declare_the_scheduler_service():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    entrypoint_bytes = (PROJECT_ROOT / "docker" / "entrypoint.sh").read_bytes()
    entrypoint = entrypoint_bytes.decode("utf-8")
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "scheduler:" in compose and 'command: ["scheduler"]' in compose
    assert "scheduler)" in entrypoint and "run_scheduler.py" in entrypoint
    assert "\r" not in entrypoint  # un script de shell con CRLF no arranca en el contenedor
    assert "SCHEDULER_ENABLED=false" in env_example
    assert "SCHEDULER_WEEKDAY=6" in env_example and "SCHEDULER_HOUR=3" in env_example
    assert (PROJECT_ROOT / "run_scheduler.py").exists()
