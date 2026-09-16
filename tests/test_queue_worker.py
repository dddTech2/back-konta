"""Pruebas unitarias para la cola de descargas y el worker con pacing de 15 minutos."""

import os
from datetime import datetime, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.database import Base
from dian_automation.db.models import User, Business, DIANExtractionJob, Invoice
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.queue.worker import ExtractionWorker


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria para tests del worker."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Sembrar usuario y dos negocios
    db = TestingSessionLocal()
    user = User(id="user-q-1", email="katerinn@test.com", full_name="Katerinn", role="ADMIN")
    db.add(user)
    biz1 = Business(id="biz-q-1", client_id=user.id, legal_name="Negocio Uno SAS", commercial_name="Negocio 1", nit="900111222", dv="1")
    biz2 = Business(id="biz-q-2", client_id=user.id, legal_name="Negocio Dos SAS", commercial_name="Negocio 2", nit="900333444", dv="2")
    db.add_all([biz1, biz2])
    db.commit()
    db.close()

    return TestingSessionLocal


def test_enqueue_and_get_runnable_job(db_session_factory):
    """Verifica el encolamiento y obtención del trabajo más antiguo listo."""
    db = db_session_factory()
    try:
        job = ExtractionQueueManager.enqueue_job(business_id="biz-q-1", target_period="2026-08", db=db)
        assert job.id is not None
        assert job.status == "ENQUEUED"
        assert job.next_run_at <= datetime.utcnow()

        # Debe estar disponible para ejecución
        runnable = ExtractionQueueManager.get_next_runnable_job(db=db)
        assert runnable is not None
        assert runnable.id == job.id
    finally:
        db.close()


def test_concurrency_one_constraint(db_session_factory):
    """Verifica que si un trabajo está en PROCESSING, ningún otro pueda ejecutarse en paralelo."""
    db = db_session_factory()
    try:
        job1 = ExtractionQueueManager.enqueue_job(business_id="biz-q-1", target_period="2026-08", db=db)
        job2 = ExtractionQueueManager.enqueue_job(business_id="biz-q-2", target_period="2026-08", db=db)

        # Marcar job1 en proceso
        ExtractionQueueManager.mark_job_processing(job_id=job1.id, db=db)

        # Consultar siguiente trabajo: debe ser None porque job1 está corriendo
        next_job = ExtractionQueueManager.get_next_runnable_job(db=db)
        assert next_job is None
    finally:
        db.close()


def test_success_pacing_fifteen_minutes(db_session_factory):
    """Verifica que tras completar una tarea con éxito, la siguiente en cola deba esperar 15 minutos."""
    db = db_session_factory()
    try:
        job1 = ExtractionQueueManager.enqueue_job(business_id="biz-q-1", target_period="2026-08", db=db)
        job2 = ExtractionQueueManager.enqueue_job(business_id="biz-q-2", target_period="2026-08", db=db)

        # Procesar job1
        ExtractionQueueManager.mark_job_processing(job_id=job1.id, db=db)

        now_before = datetime.utcnow()
        # Completar job1 con éxito (pacing default de 900s / 15m)
        ExtractionQueueManager.mark_job_success(
            job_id=job1.id,
            zip_path="downloads/fake.zip",
            db=db,
            pacing_seconds=900,
        )

        # Refrescar job2
        db.refresh(job2)

        # job2 debe haber sido reprogramado para dentro de 15 minutos
        min_expected = now_before + timedelta(seconds=895)
        assert job2.next_run_at >= min_expected
        assert job2.status == "ENQUEUED"

        # Inmediatamente después del éxito, el worker NO debe tomar job2 porque está en reposo de 15 min
        runnable = ExtractionQueueManager.get_next_runnable_job(db=db)
        assert runnable is None
    finally:
        db.close()


def test_worker_run_once_with_auto_ingest(db_session_factory):
    """Verifica que el ExtractionWorker ejecute la tarea, llame al parser y aplique el retardo."""
    db = db_session_factory()
    job = ExtractionQueueManager.enqueue_job(business_id="biz-q-1", target_period="2026-08", db=db)
    job_id = job.id
    db.close()

    real_zip = os.path.join(
        os.path.dirname(__file__), "..", "downloads", "183ff689-751a-4971-82a6-e178e427c1c3.zip"
    )

    worker = ExtractionWorker(db_session_factory=db_session_factory)

    # Mock del extractor que retorna la ruta del ZIP real
    def mock_extractor(biz, period):
        return real_zip

    # Ejecutar una iteración del worker
    result = worker.run_once(extractor_func=mock_extractor, pacing_seconds=900)

    assert result is not None
    assert result["status"] == "SUCCESS"
    assert result["job_id"] == job_id
    assert "parse_result" in result
    assert result["parse_result"]["invoices_processed"] > 0

    # Verificar que el job en BD quedó en SUCCESS
    db = db_session_factory()
    try:
        updated_job = db.query(DIANExtractionJob).filter(DIANExtractionJob.id == job_id).first()
        assert updated_job.status == "SUCCESS"
        assert updated_job.zip_path == real_zip

        # Verificar que las facturas quedaron guardadas para biz-q-1
        invoices_count = db.query(Invoice).filter(Invoice.business_id == "biz-q-1").count()
        assert invoices_count == result["parse_result"]["invoices_processed"]
    finally:
        db.close()


def test_worker_default_extractor_calls_dian_flow_empresa(db_session_factory, monkeypatch):
    """Sin extractor_func inyectado, el worker debe llamar a dian_flow.run_flow con los
    nombres de parámetro reales de su firma (target_month, login_type, representative_code,
    company_nit, person_code), en modo EMPRESA usando la cédula del representante legal,
    y usar directamente su valor de retorno como ruta del ZIP (sin buscar en disco)."""
    db = db_session_factory()
    biz = db.query(Business).filter(Business.id == "biz-q-1").first()
    biz.taxpayer_type = "PERSONA_JURIDICA"
    biz.legal_rep_doc = "10000002"
    db.commit()
    job = ExtractionQueueManager.enqueue_job(business_id="biz-q-1", target_period="2026-08", db=db)
    job_id = job.id
    db.close()

    calls = {}

    async def fake_run_flow(**kwargs):
        calls.update(kwargs)
        return "downloads/fake_from_dian_flow.zip"

    import dian_automation.dian_flow as dian_flow_module
    monkeypatch.setattr(dian_flow_module, "run_flow", fake_run_flow)

    worker = ExtractionWorker(db_session_factory=db_session_factory)
    result = worker.run_once(
        parser_func=lambda zip_path, business_id, db, job_id: {"invoices_processed": 0},
        pacing_seconds=900,
    )

    assert result["status"] == "SUCCESS"
    assert result["zip_path"] == "downloads/fake_from_dian_flow.zip"
    assert calls == {
        "target_month": "2026-08",
        "login_type": "empresa",
        "representative_code": "10000002",
        "company_nit": "900111222",
        "person_code": None,
    }


def test_worker_default_extractor_calls_dian_flow_persona(db_session_factory, monkeypatch):
    """En modo PERSONA_NATURAL, el worker debe pasar la cédula (almacenada en Business.nit)
    como person_code, sin representative_code ni company_nit."""
    db = db_session_factory()
    biz = db.query(Business).filter(Business.id == "biz-q-2").first()
    biz.taxpayer_type = "PERSONA_NATURAL"
    biz.nit = "1000000001"
    db.commit()
    job = ExtractionQueueManager.enqueue_job(business_id="biz-q-2", target_period="2026-09", db=db)
    job_id = job.id
    db.close()

    calls = {}

    async def fake_run_flow(**kwargs):
        calls.update(kwargs)
        return "downloads/fake_persona.zip"

    import dian_automation.dian_flow as dian_flow_module
    monkeypatch.setattr(dian_flow_module, "run_flow", fake_run_flow)

    worker = ExtractionWorker(db_session_factory=db_session_factory)
    result = worker.run_once(
        parser_func=lambda zip_path, business_id, db, job_id: {"invoices_processed": 0},
        pacing_seconds=900,
    )

    assert result["status"] == "SUCCESS"
    assert calls == {
        "target_month": "2026-09",
        "login_type": "persona",
        "representative_code": None,
        "company_nit": None,
        "person_code": "1000000001",
    }
