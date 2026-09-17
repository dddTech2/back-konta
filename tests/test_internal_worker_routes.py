"""Pruebas para los endpoints internos (/internal/jobs/*) que consume run_worker_remote.py.

Verifican que: (1) requieren el token compartido, (2) nunca exponen la base de datos
directamente -- todo pasa por HTTP, y (3) delegan correctamente en ExtractionQueueManager
y DIANXLSXParser, igual que hace el worker local (worker.py)."""

import io
import zipfile
import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.db.database import Base, get_db
from dian_automation.db.models import User, Business, DIANExtractionJob
from dian_automation.queue.manager import ExtractionQueueManager
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


def test_next_job_rejects_missing_or_wrong_token(client):
    res = client.get("/internal/jobs/next")
    assert res.status_code == 401

    res = client.get("/internal/jobs/next", headers=_auth_headers("token-incorrecto"))
    assert res.status_code == 401


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
