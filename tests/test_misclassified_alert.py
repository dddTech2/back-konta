"""Pruebas para la alerta de negocio posiblemente mal clasificado (AC #7 de la Story 6.3).

Verifica que:
1. Un negocio MANUAL_SALES con facturas emitidas del job avisa a los usuarios ADMIN (una vez por ADMIN)
   y el income_source del negocio NO cambia.
2. Un negocio MANUAL_SALES con solo facturas recibidas NO avisa.
3. Un negocio DIAN con facturas emitidas NO avisa.
4. Facturas emitidas de OTRO job del mismo negocio no se cuentan para el job actual.
5. Si el envío por Telegram falla, el helper devuelve False y no lanza excepciones.
6. Si no hay usuarios ADMIN con chat configurado, devuelve False y no lanza excepciones.
7. Integración (a): POST /internal/jobs/{id}/complete con ZIP y facturas emitidas avisa una sola vez;
   una segunda subida ignorada (job ya SUCCESS) no vuelve a avisar.
8. Integración (b): ExtractionWorker.execute_job con parser que inserta factura emitida avisa a los ADMIN.
"""

import io
import uuid
import zipfile
from datetime import datetime
from decimal import Decimal
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api import routes_internal_worker as internal_routes_module
from dian_automation.api.app import app
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import (
    Business,
    DIANExtractionJob,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    Invoice,
    User,
)
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.queue.worker import ExtractionWorker
from dian_automation.telegram.admin_alerts import (
    notify_admins,
    notify_misclassified_business,
)
from dian_automation.telegram.tech_ops_bot import TechOpsAlertBot

FAKE_TOKEN = "test-misclassified-token-456"


class FakeConfig:
    internal_worker_token = FAKE_TOKEN


class FakeTelegram:
    """Dispatcher simulado de Telegram para registrar llamadas."""

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
        return [
            c["data"].get("text") or c["data"].get("caption")
            for c in self.calls
            if c["data"]["chat_id"] == chat_id
        ]


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria con usuarios y negocios base."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = factory()
    client_user = User(
        id="usr-client-1",
        email="cliente@test.com",
        full_name="Cliente Principal",
        role="CLIENT",
    )
    admin_1 = User(
        id="adm-1",
        email="admin1@test.com",
        full_name="Katerinn",
        role="ADMIN",
        telegram_chat_id=1001,
        is_active=True,
    )
    admin_2 = User(
        id="adm-2",
        email="admin2@test.com",
        full_name="Admin Suplente",
        role="ADMIN",
        telegram_chat_id=1002,
        is_active=True,
    )
    admin_inactive = User(
        id="adm-off",
        email="admin_off@test.com",
        full_name="Admin Inactivo",
        role="ADMIN",
        telegram_chat_id=1003,
        is_active=False,
    )
    admin_no_chat = User(
        id="adm-nochat",
        email="admin_nochat@test.com",
        full_name="Admin Sin Chat",
        role="ADMIN",
        telegram_chat_id=None,
        is_active=True,
    )
    tech_ops = User(
        id="tech-1",
        email="tech@test.com",
        full_name="Alex TechOps",
        role="TECH_OPS",
        telegram_chat_id=2001,
        is_active=True,
    )
    db.add_all([client_user, admin_1, admin_2, admin_inactive, admin_no_chat, tech_ops])

    biz_manual = Business(
        id="biz-manual-1",
        client_id=client_user.id,
        legal_name="Comercializadora Manual SAS",
        commercial_name="Tienda Manual Express",
        nit="900123456",
        dv="8",
        taxpayer_type="PERSONA_JURIDICA",
        income_source=INCOME_SOURCE_MANUAL_SALES,
    )
    biz_dian = Business(
        id="biz-dian-1",
        client_id=client_user.id,
        legal_name="Comercializadora DIAN SAS",
        commercial_name="Tienda DIAN Total",
        nit="900987654",
        dv="3",
        taxpayer_type="PERSONA_JURIDICA",
        income_source=INCOME_SOURCE_DIAN,
    )
    db.add_all([biz_manual, biz_dian])
    db.commit()
    db.close()
    return factory


def _create_invoice(
    db,
    business_id: str,
    job_id: Optional[str],
    group_type: str = "Emitido",
    document_type: str = "Factura electrónica",
    total: Decimal = Decimal("50000.00"),
) -> Invoice:
    invoice = Invoice(
        id=str(uuid.uuid4()),
        business_id=business_id,
        job_id=job_id,
        document_type=document_type,
        cufe=f"cufe-{uuid.uuid4().hex[:16]}",
        issue_date=datetime.utcnow(),
        issuer_nit="900123456",
        issuer_name="Emisor Test",
        receiver_nit="900999000",
        receiver_name="Receptor Test",
        group_type=group_type,
        total=total,
    )
    db.add(invoice)
    db.commit()
    return invoice


# ==============================================================================================
# Pruebas Unitarias de notify_misclassified_business
# ==============================================================================================


def test_manual_sales_with_issued_invoices_notifies_admins_once_and_keeps_income_source(
    db_session_factory,
):
    """Negocio MANUAL_SALES con facturas Emitido del job -> avisa a los ADMIN una vez

    (un envío por ADMIN activo con chat) y el income_source NO cambia.
    """
    db = db_session_factory()
    try:
        telegram = FakeTelegram()
        bot = TechOpsAlertBot(bot_token="test:token", http_dispatcher=telegram)

        job = ExtractionQueueManager.enqueue_job("biz-manual-1", "2026-08", db)
        job = ExtractionQueueManager.mark_job_processing(job.id, db)

        # Crear 2 facturas emitidas y 1 recibida para este job
        _create_invoice(db, "biz-manual-1", job.id, group_type="Emitido")
        _create_invoice(db, "biz-manual-1", job.id, group_type="Emitido")
        _create_invoice(db, "biz-manual-1", job.id, group_type="Recibido")

        sent = notify_misclassified_business(db, job, bot=bot)

        assert sent is True
        # Debe enviar a admin_1 (1001) y admin_2 (1002); no al inactivo ni a tech ops
        assert telegram.chats() == {1001, 1002}
        assert len(telegram.calls) == 2

        # Verificar contenido de la alerta en español
        for chat_id in [1001, 1002]:
            texts = telegram.texts_for(chat_id)
            assert len(texts) == 1
            text = texts[0]
            assert "⚠️ Posible cliente mal clasificado:" in text
            assert "Tienda Manual Express" in text
            assert "NIT 900123456-8" in text
            assert "está registrado como ventas manuales" in text
            assert "trajo 2 factura(s) emitida(s) de la DIAN" in text
            assert "/cambiar_tipo" in text

        # El tipo del negocio NO debe haber cambiado
        business = db.query(Business).filter(Business.id == "biz-manual-1").one()
        assert business.income_source == INCOME_SOURCE_MANUAL_SALES
    finally:
        db.close()


def test_manual_sales_with_only_received_invoices_does_not_notify(db_session_factory):
    """MANUAL_SALES con solo facturas Recibido -> no avisa."""
    db = db_session_factory()
    try:
        telegram = FakeTelegram()
        bot = TechOpsAlertBot(bot_token="test:token", http_dispatcher=telegram)

        job = ExtractionQueueManager.enqueue_job("biz-manual-1", "2026-08", db)
        job = ExtractionQueueManager.mark_job_processing(job.id, db)

        # Solo facturas recibidas (gastos/compras)
        _create_invoice(db, "biz-manual-1", job.id, group_type="Recibido")
        _create_invoice(db, "biz-manual-1", job.id, group_type="Recibido")

        sent = notify_misclassified_business(db, job, bot=bot)

        assert sent is False
        assert telegram.calls == []

        business = db.query(Business).filter(Business.id == "biz-manual-1").one()
        assert business.income_source == INCOME_SOURCE_MANUAL_SALES
    finally:
        db.close()


def test_dian_business_with_issued_invoices_does_not_notify(db_session_factory):
    """Negocio DIAN con facturas Emitido -> no avisa (es el comportamiento esperado)."""
    db = db_session_factory()
    try:
        telegram = FakeTelegram()
        bot = TechOpsAlertBot(bot_token="test:token", http_dispatcher=telegram)

        job = ExtractionQueueManager.enqueue_job("biz-dian-1", "2026-08", db)
        job = ExtractionQueueManager.mark_job_processing(job.id, db)

        # Facturas emitidas normales para un negocio de tipo DIAN
        _create_invoice(db, "biz-dian-1", job.id, group_type="Emitido")

        sent = notify_misclassified_business(db, job, bot=bot)

        assert sent is False
        assert telegram.calls == []

        business = db.query(Business).filter(Business.id == "biz-dian-1").one()
        assert business.income_source == INCOME_SOURCE_DIAN
    finally:
        db.close()


def test_issued_invoices_from_other_job_do_not_count(db_session_factory):
    """Facturas Emitido de OTRO job del mismo negocio no cuentan para el job evaluado."""
    db = db_session_factory()
    try:
        telegram = FakeTelegram()
        bot = TechOpsAlertBot(bot_token="test:token", http_dispatcher=telegram)

        job_other = ExtractionQueueManager.enqueue_job("biz-manual-1", "2026-07", db)
        job_current = ExtractionQueueManager.enqueue_job("biz-manual-1", "2026-08", db)

        # Factura emitida asociada al trabajo previo (job_other)
        _create_invoice(db, "biz-manual-1", job_other.id, group_type="Emitido")

        # El trabajo actual no trajo facturas emitidas
        sent = notify_misclassified_business(db, job_current, bot=bot)

        assert sent is False
        assert telegram.calls == []
    finally:
        db.close()


def test_telegram_failure_returns_false_and_does_not_raise(db_session_factory):
    """Si el envío de Telegram falla (excepción o ok=False), el helper devuelve False y no lanza."""
    db = db_session_factory()
    try:
        telegram = FakeTelegram(fail_with=RuntimeError("Error de conexión con API Telegram"))
        bot = TechOpsAlertBot(bot_token="test:token", http_dispatcher=telegram)

        job = ExtractionQueueManager.enqueue_job("biz-manual-1", "2026-08", db)
        _create_invoice(db, "biz-manual-1", job.id, group_type="Emitido")

        # Debe capturar la excepción y devolver False
        sent = notify_misclassified_business(db, job, bot=bot)

        assert sent is False
        business = db.query(Business).filter(Business.id == "biz-manual-1").one()
        assert business.income_source == INCOME_SOURCE_MANUAL_SALES
    finally:
        db.close()


def test_no_admin_with_chat_returns_false_without_raising(db_session_factory):
    """Sin usuarios ADMIN con chat configurado, devuelve False sin lanzar excepciones."""
    db = db_session_factory()
    try:
        # Remover chat_id de todos los ADMIN
        db.query(User).filter(User.role == "ADMIN").update({"telegram_chat_id": None})
        db.commit()

        telegram = FakeTelegram()
        bot = TechOpsAlertBot(bot_token="test:token", http_dispatcher=telegram)

        job = ExtractionQueueManager.enqueue_job("biz-manual-1", "2026-08", db)
        _create_invoice(db, "biz-manual-1", job.id, group_type="Emitido")

        sent = notify_misclassified_business(db, job, bot=bot)

        assert sent is False
        assert telegram.calls == []
    finally:
        db.close()


def test_invalid_job_or_missing_business_returns_false():
    """Pasar un job inexistente o sin negocio asociado devuelve False de forma segura."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        assert notify_misclassified_business(db, None) is False

        job_orphan = DIANExtractionJob(
            id="job-orphan",
            business_id="biz-not-found",
            target_period="2026-08",
        )
        assert notify_misclassified_business(db, job_orphan) is False
    finally:
        db.close()


# ==============================================================================================
# Pruebas de Integración (a) y (b)
# ==============================================================================================


def _build_dummy_zip_bytes() -> bytes:
    """Genera un archivo ZIP en memoria con un contenido dummy."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("test.txt", "contenido dummy")
    return buf.getvalue()


def test_integration_internal_complete_endpoint_alerts_once_and_ignored_upload_does_not_re_alert(
    monkeypatch, tmp_path
):
    """Integración (a): POST /internal/jobs/{id}/complete con un ZIP válido de un negocio

    MANUAL_SALES que contiene facturas emitidas dispara el aviso una vez.
    Una segunda subida del mismo job (ya en SUCCESS, respuesta ignored) NO vuelve a avisar.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSession()

    # Sembrar admin y negocio MANUAL_SALES
    client_u = User(id="u-cli-int", email="cli@test.com", full_name="Client Int", role="CLIENT")
    admin_u = User(
        id="u-adm-int",
        email="adm@test.com",
        full_name="Admin Int",
        role="ADMIN",
        telegram_chat_id=1099,
        is_active=True,
    )
    db.add_all([client_u, admin_u])
    biz = Business(
        id="biz-man-int",
        client_id=client_u.id,
        legal_name="Comercializadora Integracion SAS",
        commercial_name="Tienda Integrada",
        nit="901234567",
        dv="5",
        taxpayer_type="PERSONA_JURIDICA",
        income_source=INCOME_SOURCE_MANUAL_SALES,
    )
    db.add(biz)
    db.commit()

    job = ExtractionQueueManager.enqueue_job("biz-man-int", "2026-08", db)
    ExtractionQueueManager.mark_job_processing(job.id, db)

    # Configurar FakeTelegram
    fake_telegram = FakeTelegram()
    fake_bot = TechOpsAlertBot(bot_token="test:token", http_dispatcher=fake_telegram)
    monkeypatch.setattr(
        "dian_automation.telegram.admin_alerts.TechOpsAlertBot",
        lambda *args, **kwargs: fake_bot,
    )

    # Simular parse_zip para que inserte una factura emitida asociada a este job_id
    def fake_parse_zip(zip_path, business_id, db, job_id):
        inv = Invoice(
            id=str(uuid.uuid4()),
            business_id=business_id,
            job_id=job_id,
            document_type="Factura electrónica",
            cufe=f"cufe-int-{uuid.uuid4().hex[:12]}",
            issue_date=datetime.utcnow(),
            issuer_nit="901234567",
            issuer_name="Tienda Integrada",
            receiver_nit="900888777",
            receiver_name="Comprador SAS",
            group_type="Emitido",
            total=Decimal("150000.00"),
        )
        db.add(inv)
        db.commit()
        return {"inserted": 1, "invoices": 1}

    monkeypatch.setattr(
        "dian_automation.api.routes_internal_worker.DIANXLSXParser.parse_zip",
        fake_parse_zip,
    )
    monkeypatch.setattr(internal_routes_module, "config", FakeConfig())
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))

    # Spy sobre notify_misclassified_business para contar llamadas exactas
    alert_call_count = 0
    real_notify = internal_routes_module.notify_misclassified_business

    def spy_notify(db_arg, job_arg, bot=None):
        nonlocal alert_call_count
        alert_call_count += 1
        return real_notify(db_arg, job_arg, bot=bot)

    monkeypatch.setattr(
        internal_routes_module, "notify_misclassified_business", spy_notify
    )

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)

    try:
        zip_bytes = _build_dummy_zip_bytes()
        headers = {"Authorization": f"Bearer {FAKE_TOKEN}"}

        # 1. Primera subida: procesa y notifica
        res1 = test_client.post(
            f"/internal/jobs/{job.id}/complete",
            headers=headers,
            files={"file": ("reporte.zip", zip_bytes, "application/zip")},
        )
        assert res1.status_code == 200
        assert res1.json()["status"] == "SUCCESS"
        assert alert_call_count == 1
        assert len(fake_telegram.calls) == 1
        assert fake_telegram.calls[0]["data"]["chat_id"] == 1099
        assert "Tienda Integrada" in fake_telegram.calls[0]["data"]["text"]
        assert "1 factura(s) emitida(s)" in fake_telegram.calls[0]["data"]["text"]

        # 2. Segunda subida del mismo job (ya SUCCESS): retorna ignored y NO vuelve a avisar
        res2 = test_client.post(
            f"/internal/jobs/{job.id}/complete",
            headers=headers,
            files={"file": ("reporte.zip", zip_bytes, "application/zip")},
        )
        assert res2.status_code == 200
        assert res2.json()["status"] == "SUCCESS"
        assert res2.json().get("ignored") is True
        # El conteo de alertas sigue siendo 1
        assert alert_call_count == 1
        assert len(fake_telegram.calls) == 1

        # El negocio permanece como MANUAL_SALES
        db.refresh(biz)
        assert biz.income_source == INCOME_SOURCE_MANUAL_SALES
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_integration_extraction_worker_execute_job_dispatches_alert(monkeypatch):
    """Integración (b): ExtractionWorker.execute_job con parser_func simulado que

    inserta una factura Emitido con job_id dispara el aviso a los administradores.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = TestingSession()
    client_u = User(id="u-cli-w", email="w_cli@test.com", full_name="Worker Client", role="CLIENT")
    admin_u = User(
        id="u-adm-w",
        email="w_adm@test.com",
        full_name="Worker Admin",
        role="ADMIN",
        telegram_chat_id=1055,
        is_active=True,
    )
    db.add_all([client_u, admin_u])
    biz = Business(
        id="biz-man-worker",
        client_id=client_u.id,
        legal_name="Comercializadora Worker SAS",
        commercial_name="Tienda Local Worker",
        nit="901888999",
        dv="4",
        taxpayer_type="PERSONA_JURIDICA",
        income_source=INCOME_SOURCE_MANUAL_SALES,
    )
    db.add(biz)
    db.commit()

    job = ExtractionQueueManager.enqueue_job("biz-man-worker", "2026-08", db)
    job_id = job.id
    db.close()

    fake_telegram = FakeTelegram()
    fake_bot = TechOpsAlertBot(bot_token="test:token", http_dispatcher=fake_telegram)
    monkeypatch.setattr(
        "dian_automation.telegram.admin_alerts.TechOpsAlertBot",
        lambda *args, **kwargs: fake_bot,
    )

    def fake_extractor(business, period):
        return "downloads/test_worker.zip"

    def fake_parser(zip_path, business_id, db_session, current_job_id):
        inv = Invoice(
            id=str(uuid.uuid4()),
            business_id=business_id,
            job_id=current_job_id,
            document_type="Factura electrónica",
            cufe=f"cufe-worker-{uuid.uuid4().hex[:12]}",
            issue_date=datetime.utcnow(),
            issuer_nit="901888999",
            issuer_name="Tienda Local Worker",
            receiver_nit="900555444",
            receiver_name="Cliente Local SAS",
            group_type="Emitido",
            total=Decimal("80000.00"),
        )
        db_session.add(inv)
        db_session.commit()
        return {"invoices": 1}

    worker = ExtractionWorker(db_session_factory=TestingSession)
    result = worker.execute_job(
        job_id=job_id,
        extractor_func=fake_extractor,
        parser_func=fake_parser,
        pacing_seconds=0,
    )

    assert result["status"] == "SUCCESS"
    assert len(fake_telegram.calls) == 1
    assert fake_telegram.calls[0]["data"]["chat_id"] == 1055
    msg = fake_telegram.calls[0]["data"]["text"]
    assert "Tienda Local Worker" in msg
    assert "NIT 901888999-4" in msg
    assert "1 factura(s) emitida(s)" in msg
    assert "/cambiar_tipo" in msg

    # Verificar que el tipo de negocio no cambió
    verify_db = TestingSession()
    biz_check = verify_db.query(Business).filter(Business.id == "biz-man-worker").one()
    assert biz_check.income_source == INCOME_SOURCE_MANUAL_SALES
    verify_db.close()
