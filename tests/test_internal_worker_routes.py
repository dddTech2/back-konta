"""Pruebas para los endpoints internos (/internal/jobs/*) que consume kontable-worker-remote.

Verifican que: (1) requieren el token compartido, (2) nunca exponen la base de datos
directamente -- todo pasa por HTTP, y (3) delegan correctamente en ExtractionQueueManager
y DIANXLSXParser, igual que hace el worker local (cli/worker.py)."""

import io
import zipfile
from datetime import datetime, timedelta

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.db.database import Base, get_db
from dian_automation.db.models import User, Business, DIANExtractionJob, WorkerHeartbeat
from dian_automation.queue.exceptions import STALE_PROCESSING_CODE
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.telegram.tech_ops_bot import TechOpsAlertBot
from dian_automation.api.app import app
from dian_automation.api import routes_internal_worker as internal_routes_module

FAKE_TOKEN = "test-shared-secret-123"


class FakeConfig:
    internal_worker_token = FAKE_TOKEN


@pytest.fixture(name="db_session")
def fixture_db_session():
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(name="client")
def fixture_client(db_session, monkeypatch):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(internal_routes_module, "config", FakeConfig())
    # Ningún test debe enviar mensajes reales aunque el .env local tenga un token de bot
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_TECH_OPS_BOT_TOKEN", raising=False)
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(name="seed_business")
def fixture_seed_business(db_session):
    user = User(id="usr-remote-1", email="remote@test.com", full_name="Remote Test", role="CLIENT")
    db_session.add(user)
    business = Business(
        id="biz-remote-1",
        client_id=user.id,
        legal_name="Empresa Remota SAS",
        commercial_name="Empresa Remota",
        nit="902033132",
        dv="1",
        taxpayer_type="PERSONA_JURIDICA",
        legal_rep_doc="1000000001",
    )
    db_session.add(business)
    db_session.commit()
    return business


def _auth_headers(token: str = FAKE_TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _build_sample_zip_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rp_Doc_Test"
    ws.append([
        "Tipo de documento", "CUFE/CUDE", "Folio", "Prefijo", "Divisa", "Forma de Pago", "Medio de Pago",
        "Fecha Emisión", "Fecha Recepción", "NIT Emisor", "Nombre Emisor", "NIT Receptor", "Nombre Receptor",
        "IVA", "ICA", "IC", "INC", "Timbre", "INC Bolsas", "IN Carbono", "IN Combustibles", "IC Datos",
        "ICL", "INPP", "IBUA", "ICUI", "Rete IVA", "Rete Renta", "Rete ICA", "Total", "Estado", "Grupo"
    ])
    excel_buf = io.BytesIO()
    wb.save(excel_buf)
    excel_buf.seek(0)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as z:
        z.writestr("test_report.xlsx", excel_buf.getvalue())
    return zip_buf.getvalue()


@pytest.mark.parametrize(
    "auth_header",
    [
        None,  # encabezado ausente
        "",  # encabezado vacío
        "token-sin-bearer",  # sin prefijo Bearer
        "Bearer ",  # Bearer vacío
        f"Bearer {FAKE_TOKEN}-extra",  # longitud distinta (más largo)
        f"Bearer {FAKE_TOKEN[:-2]}",  # longitud distinta (más corto)
        "Bearer token-incorrecto",  # token incorrecto
        "Bearer tokén".encode("latin-1"),  # carácter no ASCII: por la red llega como bytes latin-1
    ],
)
def test_next_job_rejects_missing_or_wrong_token(client, auth_header):
    headers = {"Authorization": auth_header} if auth_header is not None else {}
    res = client.get("/internal/jobs/next", headers=headers)
    assert res.status_code == 401
    assert res.json()["detail"] == "Token de worker inválido."


def test_next_job_server_unconfigured_token_returns_503(client, monkeypatch):
    class NoTokenConfig:
        internal_worker_token = None

    monkeypatch.setattr(internal_routes_module, "config", NoTokenConfig())
    res = client.get("/internal/jobs/next", headers=_auth_headers())
    assert res.status_code == 503
    assert res.json()["detail"] == "INTERNAL_WORKER_TOKEN no está configurado en el servidor."


def test_next_job_claim_loss_returns_none_and_leaves_job_unmarked(client, db_session, seed_business, monkeypatch):
    """Si claim_job pierde el reclamo (devuelve None ante concurrencia), la ruta responde
    {'job': None} y el trabajo permanece encolado sin ser marcado por esta petición."""
    job = ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)

    monkeypatch.setattr(ExtractionQueueManager, "claim_job", classmethod(lambda cls, job_id, db: None))

    res = client.get("/internal/jobs/next", headers=_auth_headers())
    assert res.status_code == 200
    assert res.json() == {"job": None}

    db_session.refresh(job)
    assert job.status == "ENQUEUED"
    assert job.started_at is None



def test_next_job_empty_queue_returns_none(client):
    res = client.get("/internal/jobs/next", headers=_auth_headers())
    assert res.status_code == 200
    assert res.json() == {"job": None}


def test_next_job_returns_and_reserves_pending_job(client, db_session, seed_business):
    ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)

    res = client.get("/internal/jobs/next", headers=_auth_headers())
    assert res.status_code == 200
    job = res.json()["job"]
    assert job["business_id"] == seed_business.id
    assert job["target_period"] == "2026-08"
    assert job["login_type"] == "empresa"
    assert job["representative_code"] == "1000000001"
    assert job["company_nit"] == "902033132"

    # El job queda reservado (PROCESSING), no disponible para otro consumidor
    db_job = db_session.query(DIANExtractionJob).filter(DIANExtractionJob.business_id == seed_business.id).first()
    assert db_job.status == "PROCESSING"

    res2 = client.get("/internal/jobs/next", headers=_auth_headers())
    assert res2.json() == {"job": None}


def test_complete_job_ingests_zip_and_marks_success(client, db_session, seed_business, tmp_path, monkeypatch):
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    job = ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)
    ExtractionQueueManager.mark_job_processing(job_id=job.id, db=db_session)

    zip_bytes = _build_sample_zip_bytes()
    res = client.post(
        f"/internal/jobs/{job.id}/complete",
        headers=_auth_headers(),
        files={"file": ("reporte.zip", zip_bytes, "application/zip")},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "SUCCESS"

    db_session.refresh(job)
    assert job.status == "SUCCESS"
    assert job.zip_path is not None


def test_fail_job_schedules_retry(client, db_session, seed_business):
    job = ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)
    ExtractionQueueManager.mark_job_processing(job_id=job.id, db=db_session)

    res = client.post(
        f"/internal/jobs/{job.id}/fail",
        headers=_auth_headers(),
        data={"error_code": "TURNSTILE_BLOCKED", "error_detail": "Timeout esperando el widget"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["attempt_count"] == 1
    assert body["status"] == "RETRY_SCHEDULED"

    db_session.refresh(job)
    assert job.status == "ENQUEUED"  # queda reprogramado, no lo perdimos
    assert job.error_code == "TURNSTILE_BLOCKED"


def test_complete_and_fail_reject_unknown_job(client):
    res = client.post(
        "/internal/jobs/no-existe/complete",
        headers=_auth_headers(),
        files={"file": ("x.zip", b"contenido", "application/zip")},
    )
    assert res.status_code == 404

    res = client.post(
        "/internal/jobs/no-existe/fail",
        headers=_auth_headers(),
        data={"error_code": "X", "error_detail": "Y"},
    )
    assert res.status_code == 404


# --- Story 1.6: fallos lentos, trabajos atascados y avisos ---------------------------------------


def _make_stale(db_session, job_id, seconds=5000):
    job = db_session.query(DIANExtractionJob).filter(DIANExtractionJob.id == job_id).one()
    job.status = "PROCESSING"
    job.started_at = datetime.utcnow() - timedelta(seconds=seconds)
    db_session.commit()
    return job


def _second_business(db_session):
    db_session.add(
        Business(
            id="biz-remote-2", client_id="usr-remote-1", legal_name="Otra Empresa SAS", commercial_name="Otra Empresa",
            nit="901777888", dv="3", taxpayer_type="PERSONA_JURIDICA", legal_rep_doc="1000000001",
        )
    )
    db_session.commit()


def _seconds_ahead(value):
    return (value - datetime.utcnow()).total_seconds()


def test_next_recovers_stale_job_and_hands_out_the_next_one(client, db_session, seed_business):
    _second_business(db_session)
    stale = ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)
    waiting = ExtractionQueueManager.enqueue_job(business_id="biz-remote-2", target_period="2026-08", db=db_session)
    _make_stale(db_session, stale.id)

    res = client.get("/internal/jobs/next", headers=_auth_headers())

    assert res.json()["job"]["job_id"] == waiting.id
    db_session.refresh(stale)
    assert stale.status == "ENQUEUED" and stale.attempt_count == 1
    assert stale.error_code == STALE_PROCESSING_CODE
    assert 21500 <= _seconds_ahead(stale.next_run_at) <= 21700


def test_next_leaves_a_recent_processing_job_alone(client, db_session, seed_business):
    job = ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)
    _make_stale(db_session, job.id, seconds=30)

    res = client.get("/internal/jobs/next", headers=_auth_headers())

    assert res.json() == {"job": None}
    db_session.refresh(job)
    assert job.status == "PROCESSING" and job.attempt_count == 0


def test_complete_accepts_a_job_already_recovered_as_stale(client, db_session, seed_business, tmp_path, monkeypatch):
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    job = ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)
    _make_stale(db_session, job.id)
    client.get("/internal/jobs/next", headers=_auth_headers())
    db_session.refresh(job)
    assert job.status == "ENQUEUED"

    res = client.post(
        f"/internal/jobs/{job.id}/complete",
        headers=_auth_headers(),
        files={"file": ("reporte.zip", _build_sample_zip_bytes(), "application/zip")},
    )

    assert res.status_code == 200
    db_session.refresh(job)
    assert job.status == "SUCCESS" and job.zip_path is not None


def test_fail_slow_error_schedules_six_hours_and_leaves_other_jobs(client, db_session, seed_business):
    _second_business(db_session)
    job = ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)
    other = ExtractionQueueManager.enqueue_job(business_id="biz-remote-2", target_period="2026-08", db=db_session)
    other_next_run = other.next_run_at
    ExtractionQueueManager.mark_job_processing(job_id=job.id, db=db_session)

    res = client.post(
        f"/internal/jobs/{job.id}/fail",
        headers=_auth_headers(),
        data={"error_code": "ExportTimeoutError", "error_detail": "El reporte no quedó listo"},
    )

    assert res.status_code == 200
    assert res.json() == {"status": "RETRY_SCHEDULED", "job_id": job.id, "attempt_count": 1}
    db_session.refresh(job)
    db_session.refresh(other)
    assert job.status == "ENQUEUED" and 21500 <= _seconds_ahead(job.next_run_at) <= 21700
    assert other.next_run_at == other_next_run


@pytest.mark.parametrize("state", ["ENQUEUED", "SUCCESS", "FAILED"])
def test_late_fail_on_a_job_no_longer_processing_changes_nothing(client, db_session, seed_business, state):
    job = ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)
    job.status = state
    db_session.commit()
    next_run_at = job.next_run_at

    res = client.post(
        f"/internal/jobs/{job.id}/fail",
        headers=_auth_headers(),
        data={"error_code": "ExportTimeoutError", "error_detail": "tardío"},
        files={"screenshot": ("captura.png", b"png", "image/png")},
    )

    assert res.status_code == 200
    assert res.json() == {"status": state, "job_id": job.id, "attempt_count": 0, "ignored": True}
    db_session.refresh(job)
    assert job.status == state and job.attempt_count == 0 and job.next_run_at == next_run_at
    assert job.error_code is None and job.screenshot_path is None


@pytest.fixture(name="recorded_alerts")
def fixture_recorded_alerts(monkeypatch):
    """Simula la creación del callback de avisos y registra cada invocación."""
    calls = []

    def fake_factory(bot=None, db_session_factory=None):
        return lambda job, code, detail, screenshot: calls.append((job.id, code, job.status))

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TEST")
    monkeypatch.setattr(internal_routes_module, "create_failure_alert_callback", fake_factory)
    return calls


def test_fail_and_stale_recovery_trigger_the_alert_callback(client, db_session, seed_business, recorded_alerts):
    _second_business(db_session)
    stale = ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)
    current = ExtractionQueueManager.enqueue_job(business_id="biz-remote-2", target_period="2026-08", db=db_session)
    _make_stale(db_session, stale.id)

    client.get("/internal/jobs/next", headers=_auth_headers())  # recupera `stale` y entrega `current`
    client.post(
        f"/internal/jobs/{current.id}/fail",
        headers=_auth_headers(),
        data={"error_code": "AUTH_FAILED", "error_detail": "credenciales"},
    )
    client.post(  # tardío: ya no está PROCESSING, no debe avisar
        f"/internal/jobs/{current.id}/fail",
        headers=_auth_headers(),
        data={"error_code": "AUTH_FAILED", "error_detail": "credenciales"},
    )

    assert recorded_alerts == [
        (stale.id, STALE_PROCESSING_CODE, "ENQUEUED"),
        (current.id, "AUTH_FAILED", "ENQUEUED"),
    ]


def test_fail_notifies_admin_end_to_end(client, db_session, seed_business, monkeypatch):
    """Cablea la ruta con el callback real: un fallo lento llega a la administradora, no a TECH_OPS."""
    from sqlalchemy.orm import sessionmaker

    db_session.add_all(
        [
            User(id="adm-r", email="adm@test.com", full_name="Katerinn", role="ADMIN", telegram_chat_id=1001),
            User(id="tech-r", email="tech@test.com", full_name="Alex", role="TECH_OPS", telegram_chat_id=2001),
        ]
    )
    db_session.commit()
    sent = []

    def fake_dispatcher(endpoint, data, files):
        sent.append((endpoint, data))
        return {"ok": True}

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TEST")
    monkeypatch.setattr(
        internal_routes_module, "TechOpsAlertBot",
        lambda bot_token=None: TechOpsAlertBot(bot_token=bot_token, http_dispatcher=fake_dispatcher),
    )
    monkeypatch.setattr(internal_routes_module, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    job = ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)
    ExtractionQueueManager.mark_job_processing(job_id=job.id, db=db_session)

    res = client.post(
        f"/internal/jobs/{job.id}/fail",
        headers=_auth_headers(),
        data={"error_code": "ExportTimeoutError", "error_detail": "no quedó listo"},
    )

    assert res.status_code == 200
    assert [d["chat_id"] for _, d in sent] == [1001]
    text = sent[0][1]["text"]
    assert "Empresa Remota" in text and "902033132-1" in text and "2026-08" in text and "Intento 1 de 3" in text


# --- Latido del worker (Story 1.8) ------------------------------------------------------------


def _heartbeats(db_session):
    db_session.expire_all()
    return {w.name: w for w in db_session.query(WorkerHeartbeat).all()}


def test_first_next_creates_the_heartbeat_row_with_the_header_name(client, db_session):
    before = datetime.utcnow()

    res = client.get("/internal/jobs/next", headers={**_auth_headers(), "X-Worker-Name": "casa"})

    assert res.status_code == 200
    rows = _heartbeats(db_session)
    assert list(rows) == ["casa"]
    assert before <= rows["casa"].last_seen_at <= datetime.utcnow()
    assert rows["casa"].last_alert_at is None


def test_next_without_header_uses_the_default_name(client, db_session):
    client.get("/internal/jobs/next", headers=_auth_headers())
    client.get("/internal/jobs/next", headers={**_auth_headers(), "X-Worker-Name": "   "})

    assert list(_heartbeats(db_session)) == ["remote"]


def test_next_updates_last_seen_and_clears_the_alert_mark(client, db_session):
    old = datetime.utcnow() - timedelta(hours=3)
    db_session.add(WorkerHeartbeat(name="remote", last_seen_at=old, last_alert_at=old))
    db_session.commit()

    client.get("/internal/jobs/next", headers=_auth_headers())

    rows = _heartbeats(db_session)
    assert len(rows) == 1
    assert rows["remote"].last_seen_at > old + timedelta(hours=2)
    assert rows["remote"].last_alert_at is None


def test_next_records_the_heartbeat_even_when_a_job_is_delivered(client, db_session, seed_business):
    ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)

    res = client.get("/internal/jobs/next", headers=_auth_headers())

    assert res.json()["job"] is not None
    assert "remote" in _heartbeats(db_session)


def test_next_rejects_a_bad_token_without_recording_a_heartbeat(client, db_session):
    client.get("/internal/jobs/next", headers=_auth_headers("otro"))

    assert _heartbeats(db_session) == {}


def test_next_still_delivers_the_job_when_the_heartbeat_cannot_be_saved(client, db_session, seed_business, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("base caída")

    monkeypatch.setattr(internal_routes_module, "record_heartbeat", boom)
    ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)

    res = client.get("/internal/jobs/next", headers=_auth_headers())

    assert res.status_code == 200
    assert res.json()["job"]["business_id"] == seed_business.id


def test_complete_on_a_job_already_success_is_ignored_and_does_not_respace_the_queue(
    client, db_session, seed_business, tmp_path, monkeypatch
):
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    _second_business(db_session)
    done = ExtractionQueueManager.enqueue_job(business_id=seed_business.id, target_period="2026-08", db=db_session)
    ExtractionQueueManager.mark_job_processing(job_id=done.id, db=db_session)
    ExtractionQueueManager.mark_job_success(job_id=done.id, zip_path="previo.zip", db=db_session)
    waiting = ExtractionQueueManager.enqueue_job(business_id="biz-remote-2", target_period="2026-08", db=db_session)
    next_run_before = waiting.next_run_at
    finished_before = done.finished_at

    res = client.post(
        f"/internal/jobs/{done.id}/complete",
        headers=_auth_headers(),
        files={"file": ("reporte.zip", _build_sample_zip_bytes(), "application/zip")},
    )

    assert res.status_code == 200
    assert res.json() == {"status": "SUCCESS", "job_id": done.id, "ignored": True}
    db_session.refresh(done)
    db_session.refresh(waiting)
    assert done.zip_path == "previo.zip" and done.finished_at == finished_before
    assert waiting.next_run_at == next_run_before
    assert not list(tmp_path.iterdir())
