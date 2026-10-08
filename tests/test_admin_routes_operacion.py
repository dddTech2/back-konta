"""Pruebas de integración para las rutas de operación de administración (Story 8.4).

Cubre:
1. Seguridad: cliente regular -> 403 Forbidden en todas las rutas; anónimo -> 401.
2. Resumen (GET /api/admin/summary) con datos sembrados: estados, cortes en 7 días, gracia, pagos del mes, fallos 24 h.
3. Extracciones DIAN (GET /api/admin/jobs): ordenamiento, filtros por estado y negocio, mensajes de error legibles, sin zip_path ni screenshot_path.
4. Estado del worker (GET /api/admin/worker): silencioso y no silencioso con umbral paramétrico.
5. Lanzar extracciones (POST /api/admin/clients/{biz}/extractions): un mes exacto, rango de 3 meses, validación 1-12.
6. Carga de documentos (POST /api/admin/clients/{biz}/documents): PDF válido, archivo falso (415), archivo grande (413), OTRO sin descripción (422), aviso Telegram.
7. Retiro de documentos (DELETE /api/admin/clients/{biz}/documents/{doc_id}): 204 lógico, reintento 409 Conflict.
8. Enlace de descarga (POST .../link y GET /api/documents/file/{token}): token usado por ADMIN activo sin suscripción.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
import pytest

from dian_automation.config import config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from zoneinfo import ZoneInfo

from dian_automation.api.app import app
from dian_automation.core import document_service
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import (
    Business,
    BusinessDocument,
    DIANExtractionJob,
    PaymentRecord,
    Subscription,
    User,
    WorkerHeartbeat,
)
from dian_automation.telegram.admin_alerts import _cause_text


_BOGOTA_TZ = ZoneInfo("America/Bogota")


@pytest.fixture(name="db_session")
def fixture_db_session():
    """Base de datos SQLite en memoria para pruebas de administración de operaciones."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def fake_bot_username(monkeypatch):
    """Nombre del bot estático para evitar llamadas a Telegram o variables de entorno."""
    from dian_automation.api import routes_admin

    monkeypatch.setattr(routes_admin, "_resolve_bot_username", lambda: "KontaPruebasBot")


@pytest.fixture
def telegram_outbox(monkeypatch):
    """Intercepta las notificaciones salientes de Telegram para verificar texto y destinatario."""
    from dian_automation.api import routes_admin

    sent = []

    def mock_send(chat_id: int, text: str, parse_mode=None):
        sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
        return True

    monkeypatch.setattr(routes_admin, "send_telegram_message", mock_send)
    return sent


@pytest.fixture(name="client_app")
def fixture_client_app(db_session):
    """TestClient configurado con override de la base de datos."""
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def admin_user(db_session):
    """Administrador activo autorizado."""
    admin = User(
        id="usr-admin-84",
        email="katerinn.admin@konta.co",
        full_name="Katerinn Administradora",
        role="ADMIN",
        is_active=True,
        telegram_chat_id=999888111,
    )
    db_session.add(admin)
    db_session.commit()
    return admin


@pytest.fixture
def regular_client_user(db_session):
    """Usuario cliente regular no administrativo."""
    client = User(
        id="usr-client-regular-84",
        email="cliente.regular@empresa.co",
        full_name="Cliente Regular",
        role="CLIENT",
        is_active=True,
        telegram_chat_id=123456789,
        is_telegram_linked=True,
    )
    db_session.add(client)
    db_session.commit()
    return client


@pytest.fixture
def admin_headers(bearer, admin_user):
    """Cabeceras Bearer válidas de administrador."""
    return bearer(admin_user.id)


@pytest.fixture
def client_headers(bearer, regular_client_user):
    """Cabeceras Bearer de usuario cliente (no admin)."""
    return bearer(regular_client_user.id)


@pytest.fixture(autouse=True)
def setup_documents_dir(tmp_path, monkeypatch):
    """Aísla el directorio de documentos en tmp_path usando document_service.config mutable."""
    docs_dir = tmp_path / "konta_docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(document_service.config, "documents_dir", str(docs_dir))
    return docs_dir


# ==============================================================================
# Escenario 1: Seguridad — cliente regular -> 403; anónimo -> 401
# ==============================================================================


def test_operacion_routes_forbidden_for_clients_and_unauthenticated(client_app, client_headers):
    """Un usuario con rol CLIENT recibe 403 Forbidden y un anónimo 401 en todas las rutas 8.4."""
    routes_to_test = [
        ("GET", "/api/admin/summary", None),
        ("GET", "/api/admin/jobs", None),
        ("GET", "/api/admin/worker", None),
        ("POST", "/api/admin/clients/biz-123/extractions", {"months": 3}),
        ("GET", "/api/admin/clients/biz-123/documents", None),
        ("DELETE", "/api/admin/clients/biz-123/documents/doc-123", None),
        ("POST", "/api/admin/clients/biz-123/documents/doc-123/link", None),
    ]

    for method, path, payload in routes_to_test:
        # Rol CLIENT -> 403
        resp_client = client_app.request(method, path, headers=client_headers, json=payload or {})
        assert resp_client.status_code == 403, f"{method} {path} esperado 403, obtuvo {resp_client.status_code}"
        assert resp_client.json()["detail"] == "Acceso solo para la administración."

        # Sin autenticación -> 401
        resp_anon = client_app.request(method, path, json=payload or {})
        assert resp_anon.status_code == 401, f"{method} {path} esperado 401, obtuvo {resp_anon.status_code}"


# ==============================================================================
# Escenario 2: Resumen con datos sembrados (AC #1)
# ==============================================================================


def test_admin_summary_with_seeded_data(client_app, admin_headers, db_session):
    """GET /api/admin/summary consolida estados de clientes, cortes a 7 días, gracia, pagos y fallos 24 h."""
    now_bogota = datetime.now(_BOGOTA_TZ)
    today = now_bogota.date()

    # 1. Cliente 1: ACTIVO, corte en 3 días (próximos 7 días), origen DIAN
    u1 = User(id="u1", email="u1@test.co", full_name="Cliente Uno", role="CLIENT", is_active=True, is_telegram_linked=True, telegram_chat_id=111)
    b1 = Business(id="b1", client_id=u1.id, legal_name="Negocio 1", commercial_name="Comercial Uno", nit="900000001", dv="1", income_source="DIAN")
    s1 = Subscription(
        id="s1", client_id=u1.id, plan="TRIMESTRAL", status="ACTIVO",
        discount_rate=Decimal("5.0"), base_price=Decimal("150000"), final_price=Decimal("142500"),
        start_date=today - timedelta(days=87),
        cutoff_date=today + timedelta(days=3),
        grace_period_end=today + timedelta(days=6),
    )

    # 2. Cliente 2: EN_MORA, en periodo de gracia, origen MANUAL_SALES
    u2 = User(id="u2", email="u2@test.co", full_name="Cliente Dos", role="CLIENT", is_active=True, is_telegram_linked=True, telegram_chat_id=222)
    b2 = Business(id="b2", client_id=u2.id, legal_name="Negocio 2", commercial_name="Comercial Dos", nit="900000002", dv="2", income_source="MANUAL_SALES")
    s2 = Subscription(
        id="s2", client_id=u2.id, plan="SEMESTRAL", status="EN_MORA",
        discount_rate=Decimal("8.0"), base_price=Decimal("300000"), final_price=Decimal("276000"),
        start_date=today - timedelta(days=182),
        cutoff_date=today - timedelta(days=2),
        grace_period_end=today + timedelta(days=1),
    )

    # 3. Cliente 3: BLOQUEADO, sin Telegram vinculado
    u3 = User(id="u3", email="u3@test.co", full_name="Cliente Tres", role="CLIENT", is_active=True, is_telegram_linked=False, telegram_chat_id=None)
    b3 = Business(id="b3", client_id=u3.id, legal_name="Negocio 3", commercial_name="Comercial Tres", nit="900000003", dv="3", income_source="DIAN")
    s3 = Subscription(
        id="s3", client_id=u3.id, plan="ANUAL", status="BLOQUEADO",
        discount_rate=Decimal("10.0"), base_price=Decimal("600000"), final_price=Decimal("540000"),
        start_date=today - timedelta(days=380),
        cutoff_date=today - timedelta(days=15),
        grace_period_end=today - timedelta(days=12),
    )

    # 4. Cliente 4: CANCELADO
    u4 = User(id="u4", email="u4@test.co", full_name="Cliente Cuatro", role="CLIENT", is_active=True, is_telegram_linked=True, telegram_chat_id=444)
    b4 = Business(id="b4", client_id=u4.id, legal_name="Negocio 4", commercial_name="Comercial Cuatro", nit="900000004", dv="4", income_source="DIAN")
    s4 = Subscription(
        id="s4", client_id=u4.id, plan="TRIMESTRAL", status="CANCELADO",
        discount_rate=Decimal("5.0"), base_price=Decimal("150000"), final_price=Decimal("142500"),
        start_date=today - timedelta(days=200),
        cutoff_date=today - timedelta(days=110),
        grace_period_end=today - timedelta(days=107),
    )

    # 5. Cliente 5: SIN_SUSCRIPCION
    u5 = User(id="u5", email="u5@test.co", full_name="Cliente Cinco", role="CLIENT", is_active=True, is_telegram_linked=True, telegram_chat_id=555)
    b5 = Business(id="b5", client_id=u5.id, legal_name="Negocio 5", commercial_name="Comercial Cinco", nit="900000005", dv="5", income_source="DIAN")

    # 6. Pagos del mes en curso y de mes anterior
    p1 = PaymentRecord(id="p1", subscription_id=s1.id, amount=Decimal("142500"), payment_date=date(today.year, today.month, 1), reference_code="REF-01")
    p2 = PaymentRecord(id="p2", subscription_id=s2.id, amount=Decimal("100000"), payment_date=today, reference_code="REF-02")
    prev_month_date = today - timedelta(days=45)
    p_old = PaymentRecord(id="p_old", subscription_id=s3.id, amount=Decimal("540000"), payment_date=prev_month_date, reference_code="REF-OLD")

    # 7. Trabajos fallidos en últimas 24h y antiguos
    now_utc = datetime.utcnow()
    j_failed_recent = DIANExtractionJob(
        id="job-f1", business_id=b1.id, target_period="2026-08", status="FAILED",
        error_code="EXPORT_TIMEOUT", finished_at=now_utc - timedelta(hours=3),
    )
    j_failed_old = DIANExtractionJob(
        id="job-f2", business_id=b2.id, target_period="2026-07", status="FAILED",
        error_code="EXPORT_TIMEOUT", finished_at=now_utc - timedelta(hours=30),
    )
    j_success = DIANExtractionJob(
        id="job-ok", business_id=b1.id, target_period="2026-08", status="SUCCESS",
        finished_at=now_utc - timedelta(hours=1),
    )

    # 8. Worker heartbeat
    wh = WorkerHeartbeat(id="wh-1", name="remote", last_seen_at=now_utc - timedelta(minutes=4))

    db_session.add_all([
        u1, b1, s1, u2, b2, s2, u3, b3, s3, u4, b4, s4, u5, b5,
        p1, p2, p_old, j_failed_recent, j_failed_old, j_success, wh
    ])
    db_session.commit()

    resp = client_app.get("/api/admin/summary", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()

    # Validación de clientes por estado
    c_status = data["clients_by_status"]
    assert c_status["ACTIVO"] == 1
    assert c_status["EN_MORA"] == 1
    assert c_status["BLOQUEADO"] == 1
    assert c_status["CANCELADO"] == 1
    assert c_status["SIN_SUSCRIPCION"] == 1

    # Clientes por origen de ingresos
    c_source = data["clients_by_income_source"]
    assert c_source["DIAN"] == 4
    assert c_source["MANUAL_SALES"] == 1

    # Próximos cortes (próximos 7 días)
    cutoffs = data["upcoming_cutoffs"]
    assert len(cutoffs) == 1
    assert cutoffs[0]["business_id"] == "b1"
    assert cutoffs[0]["commercial_name"] == "Comercial Uno"
    assert cutoffs[0]["nit"] == "900000001"
    assert cutoffs[0]["cutoff_date"] == (today + timedelta(days=3)).isoformat()

    # En periodo de gracia
    grace = data["in_grace"]
    assert len(grace) == 1
    assert grace[0]["business_id"] == "b2"
    assert grace[0]["commercial_name"] == "Comercial Dos"
    assert grace[0]["nit"] == "900000002"
    assert grace[0]["grace_period_end"] == (today + timedelta(days=1)).isoformat()

    # Pagos de este mes
    payments_info = data["payments_this_month"]
    assert payments_info["count"] == 2
    assert payments_info["total"] == 242500.0

    # Fallos 24 h
    assert data["failed_jobs_24h"] == 1

    # Telegram sin vincular
    assert data["unlinked_telegram"] >= 1

    # Worker
    assert data["worker"]["silence_threshold_minutes"] == config.worker_silence_minutes
    assert len(data["worker"]["workers"]) == 1
    assert data["worker"]["workers"][0]["name"] == "remote"
    assert data["worker"]["workers"][0]["is_silent"] is False
    assert data["worker"]["workers"][0]["minutes_since"] == 4


# ==============================================================================
# Escenario 3: Filtros de jobs y mensajes legibles (AC #2)
# ==============================================================================


def test_admin_jobs_filters_and_human_messages(client_app, admin_headers, db_session):
    """GET /api/admin/jobs devuelve trabajos con paginación, filtros y mensajes legibles sin paths confidenciales."""
    u = User(id="usr-j1", email="jobs@test.co", full_name="User Jobs", role="CLIENT", is_active=True)
    b1 = Business(id="biz-j1", client_id=u.id, legal_name="Empresa Jobs 1", commercial_name="Jobs 1", nit="901000111", dv="1")
    b2 = Business(id="biz-j2", client_id=u.id, legal_name="Empresa Jobs 2", commercial_name="Jobs 2", nit="901000222", dv="2")

    t1 = datetime(2026, 8, 1, 10, 0, 0)
    t2 = datetime(2026, 8, 2, 10, 0, 0)
    t3 = datetime(2026, 8, 3, 10, 0, 0)
    t4 = datetime(2026, 8, 4, 10, 0, 0)

    # Job 1: Éxito
    j1 = DIANExtractionJob(
        id="j-1", business_id=b1.id, target_period="2026-05", status="SUCCESS",
        attempt_count=1, max_attempts=3, created_at=t1, finished_at=t1,
        zip_path="/secret/path/j1.zip", screenshot_path="/secret/shot1.png",
    )
    # Job 2: Falla lenta con código cubierto por _cause_text
    j2 = DIANExtractionJob(
        id="j-2", business_id=b1.id, target_period="2026-06", status="FAILED",
        attempt_count=3, max_attempts=3, error_code="EXPORT_TIMEOUT",
        created_at=t2, finished_at=t2,
        zip_path="/secret/path/j2.zip", screenshot_path="/secret/shot2.png",
    )
    # Job 3: Falla con código conocido de autenticación
    j3 = DIANExtractionJob(
        id="j-3", business_id=b2.id, target_period="2026-07", status="FAILED",
        attempt_count=1, max_attempts=3, error_code="AUTH_FAILED",
        created_at=t3, finished_at=t3,
    )
    # Job 4: Falla con código desconocido
    j4 = DIANExtractionJob(
        id="j-4", business_id=b2.id, target_period="2026-08", status="FAILED",
        attempt_count=1, max_attempts=3, error_code="PORTAL_GLITCH_XYZ",
        created_at=t4, finished_at=t4,
    )

    db_session.add_all([u, b1, b2, j1, j2, j3, j4])
    db_session.commit()

    # 1. Consulta general: más reciente primero (j4, j3, j2, j1)
    resp = client_app.get("/api/admin/jobs", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4
    items = data["items"]
    assert len(items) == 4
    assert items[0]["job_id"] == "j-4"
    assert items[1]["job_id"] == "j-3"
    assert items[2]["job_id"] == "j-2"
    assert items[3]["job_id"] == "j-1"

    # Verificar que NO se expongan zip_path ni screenshot_path
    for item in items:
        assert "zip_path" not in item
        assert "screenshot_path" not in item

    # Validación de mensajes legibles
    # j4 (desconocido)
    assert items[0]["error_code"] == "PORTAL_GLITCH_XYZ"
    assert items[0]["error_message"] == "Error en la descarga (PORTAL_GLITCH_XYZ)"

    # j3 (código conocido)
    assert items[1]["error_code"] == "AUTH_FAILED"
    assert "autenticación" in items[1]["error_message"].lower()

    # j2 (descarga lenta/atascada vía _cause_text)
    assert items[2]["error_code"] == "EXPORT_TIMEOUT"
    expected_cause = _cause_text("EXPORT_TIMEOUT")
    assert expected_cause in items[2]["error_message"]

    # j1 (sin error)
    assert items[3]["error_code"] is None
    assert items[3]["error_message"] is None

    # 2. Filtro por status
    r_filter_status = client_app.get("/api/admin/jobs?status=SUCCESS", headers=admin_headers)
    assert r_filter_status.status_code == 200
    data_success = r_filter_status.json()
    assert data_success["total"] == 1
    assert data_success["items"][0]["job_id"] == "j-1"

    # 3. Filtro por business_id
    r_filter_biz = client_app.get(f"/api/admin/jobs?business_id={b1.id}", headers=admin_headers)
    assert r_filter_biz.status_code == 200
    data_biz = r_filter_biz.json()
    assert data_biz["total"] == 2
    assert {it["job_id"] for it in data_biz["items"]} == {"j-1", "j-2"}

    # 4. Paginación
    r_page = client_app.get("/api/admin/jobs?page=1&page_size=2", headers=admin_headers)
    assert r_page.status_code == 200
    data_paged = r_page.json()
    assert data_paged["total"] == 4
    assert len(data_paged["items"]) == 2
    assert data_paged["page"] == 1
    assert data_paged["page_size"] == 2


# ==============================================================================
# Escenario 4: Worker silencioso y no silencioso (AC #3)
# ==============================================================================


def test_admin_worker_status_silent_and_alive(client_app, admin_headers, db_session):
    """GET /api/admin/worker reporta workers activos y caídos según umbral de silencio."""
    now_utc = datetime.utcnow()

    # Worker 1: Reportó hace 5 minutos (vivo)
    w1 = WorkerHeartbeat(id="w1", name="worker-residencial-1", last_seen_at=now_utc - timedelta(minutes=5))
    # Worker 2: Reportó hace 45 minutos (silencioso: el umbral por defecto es 15 min)
    w2 = WorkerHeartbeat(id="w2", name="worker-residencial-2", last_seen_at=now_utc - timedelta(minutes=45))
    # Worker 3: Nunca reportó
    w3 = WorkerHeartbeat(id="w3", name="worker-nunca-visto", last_seen_at=None)

    db_session.add_all([w1, w2, w3])
    db_session.commit()

    resp = client_app.get("/api/admin/worker", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    from dian_automation.config import config

    assert data["silence_threshold_minutes"] == config.worker_silence_minutes

    workers_by_name = {w["name"]: w for w in data["workers"]}
    assert len(workers_by_name) == 3

    # w1
    assert workers_by_name["worker-residencial-1"]["is_silent"] is False
    assert workers_by_name["worker-residencial-1"]["minutes_since"] == 5

    # w2
    assert workers_by_name["worker-residencial-2"]["is_silent"] is True
    assert workers_by_name["worker-residencial-2"]["minutes_since"] == 45

    # w3
    assert workers_by_name["worker-nunca-visto"]["is_silent"] is True
    assert workers_by_name["worker-nunca-visto"]["minutes_since"] is None
    assert workers_by_name["worker-nunca-visto"]["last_seen_at"] is None


# ==============================================================================
# Escenario 5: Lanzar extracciones (AC #4)
# ==============================================================================


def test_admin_enqueue_extractions_single_month_and_range(client_app, admin_headers, db_session):
    """POST /api/admin/clients/{biz}/extractions encola trabajos para un mes y para rango de meses."""
    u = User(id="usr-ext", email="ext@test.co", full_name="User Ext", role="CLIENT", is_active=True)
    b = Business(id="biz-ext-1", client_id=u.id, legal_name="Ext S.A.", commercial_name="Ext Comercial", nit="900555666", dv="7")
    db_session.add_all([u, b])
    db_session.commit()

    # 1. Extracción de un mes exacto
    r1 = client_app.post(
        f"/api/admin/clients/{b.id}/extractions",
        headers=admin_headers,
        json={"period": "2026-08"},
    )
    assert r1.status_code == 201
    d1 = r1.json()
    assert d1["business_id"] == b.id
    assert d1["target_period"] == "2026-08"
    assert d1["status"] == "ENQUEUED"
    assert d1["job_id"] is not None

    # 2. Extracción de 3 meses (rango)
    r2 = client_app.post(
        f"/api/admin/clients/{b.id}/extractions",
        headers=admin_headers,
        json={"months": 3},
    )
    assert r2.status_code == 201
    d2 = r2.json()
    assert " - " in d2["target_period"]
    assert d2["status"] == "ENQUEUED"

    # 3. Validación de meses fuera de rango (1-12)
    r_bad_months = client_app.post(
        f"/api/admin/clients/{b.id}/extractions",
        headers=admin_headers,
        json={"months": 15},
    )
    assert r_bad_months.status_code == 422
    assert r_bad_months.json()["code"] == "INVALID_MONTHS_RANGE"

    # 4. Validación de periodo con formato inválido
    r_bad_period = client_app.post(
        f"/api/admin/clients/{b.id}/extractions",
        headers=admin_headers,
        json={"period": "2026/08"},
    )
    assert r_bad_period.status_code == 422
    assert r_bad_period.json()["code"] == "INVALID_PERIOD_FORMAT"

    # 5. Negocio inexistente -> 404
    r_no_biz = client_app.post(
        "/api/admin/clients/biz-no-existe/extractions",
        headers=admin_headers,
        json={"period": "2026-08"},
    )
    assert r_no_biz.status_code == 404
    assert r_no_biz.json()["code"] == "BUSINESS_NOT_FOUND"


# ==============================================================================
# Escenario 6: Documentos — Subir PDF, falsos (415), grandes (413), OTRO sin desc (422) (AC #5, #6)
# ==============================================================================


def test_admin_upload_documents_validations_and_telegram(
    client_app, admin_headers, db_session, telegram_outbox, monkeypatch
):
    """POST /api/admin/clients/{biz}/documents valida tamaño, formato y campos de formulario, notificando al cliente."""
    u = User(
        id="usr-doc-owner", email="owner@test.co", full_name="Carlos Dueño",
        role="CLIENT", is_active=True, is_telegram_linked=True, telegram_chat_id=888777666,
    )
    b = Business(id="biz-docs", client_id=u.id, legal_name="Doc Corp", commercial_name="Doc Corp SAS", nit="900999888", dv="9")
    db_session.add_all([u, b])
    db_session.commit()

    pdf_bytes = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\nxref\n0 1\ntrailer<</Root 1 0 R>>\n%%EOF"

    # 1. Carga válida de RUT en PDF
    r_pdf = client_app.post(
        f"/api/admin/clients/{b.id}/documents",
        headers=admin_headers,
        data={"doc_type": "RUT"},
        files={"file": ("rut_empresa.pdf", pdf_bytes, "application/pdf")},
    )
    assert r_pdf.status_code == 201
    d_pdf = r_pdf.json()
    assert d_pdf["doc_type"] == "RUT"
    assert d_pdf["number"] == 1
    assert d_pdf["original_filename"] == "rut_empresa.pdf"

    # Comprobar aviso de Telegram saliente
    assert len(telegram_outbox) == 1
    assert telegram_outbox[0]["chat_id"] == 888777666
    assert "Katerinn cargó tu RUT en tu panel" in telegram_outbox[0]["text"]

    # 2. Archivo falso (contenido no PDF/PNG/JPEG) -> 415
    fake_bytes = b"Este es un archivo de texto disfrazado de PDF"
    r_fake = client_app.post(
        f"/api/admin/clients/{b.id}/documents",
        headers=admin_headers,
        data={"doc_type": "CAMARA"},
        files={"file": ("falso.pdf", fake_bytes, "application/pdf")},
    )
    assert r_fake.status_code == 415
    assert r_fake.json()["code"] == "INVALID_CONTENT_TYPE"

    # 3. Archivo que supera el tamaño máximo permitido -> 413
    monkeypatch.setattr(document_service.config, "document_max_mb", 1)
    huge_bytes = b"%PDF-1.4 " + b"0" * (1024 * 1024 + 1024)
    r_huge = client_app.post(
        f"/api/admin/clients/{b.id}/documents",
        headers=admin_headers,
        data={"doc_type": "BANCARIA"},
        files={"file": ("pesado.pdf", huge_bytes, "application/pdf")},
    )
    assert r_huge.status_code == 413
    assert r_huge.json()["code"] == "FILE_TOO_LARGE"

    # Restaurar tamaño
    monkeypatch.setattr(document_service.config, "document_max_mb", 10)

    # 4. Tipo OTRO sin descripción -> 422
    r_otro_no_desc = client_app.post(
        f"/api/admin/clients/{b.id}/documents",
        headers=admin_headers,
        data={"doc_type": "OTRO"},
        files={"file": ("otro.pdf", pdf_bytes, "application/pdf")},
    )
    assert r_otro_no_desc.status_code == 422
    assert r_otro_no_desc.json()["code"] == "DESCRIPTION_REQUIRED_FOR_OTHER"

    # 5. Tipo OTRO con descripción -> 201
    r_otro_ok = client_app.post(
        f"/api/admin/clients/{b.id}/documents",
        headers=admin_headers,
        data={"doc_type": "OTRO", "description": "Contrato comercial 2026"},
        files={"file": ("otro.pdf", pdf_bytes, "application/pdf")},
    )
    assert r_otro_ok.status_code == 201
    d_otro = r_otro_ok.json()
    assert d_otro["doc_type"] == "OTRO"
    assert d_otro["description"] == "Contrato comercial 2026"
    assert d_otro["number"] == 2

    # 6. Tipo de documento no reconocido -> 422
    r_bad_type = client_app.post(
        f"/api/admin/clients/{b.id}/documents",
        headers=admin_headers,
        data={"doc_type": "TIPO_INVENTADO"},
        files={"file": ("doc.pdf", pdf_bytes, "application/pdf")},
    )
    assert r_bad_type.status_code == 422
    assert r_bad_type.json()["code"] == "INVALID_DOC_TYPE"


# ==============================================================================
# Escenario 7: Listar y Retirar dos veces (409) (AC #5)
# ==============================================================================


def test_admin_list_and_retire_documents(client_app, admin_headers, db_session):
    """GET lista activos con número estable; DELETE retira (204) y el reintento responde 409 Conflict."""
    u = User(id="usr-retire", email="retire@test.co", full_name="User Retire", role="CLIENT", is_active=True)
    b = Business(id="biz-retire", client_id=u.id, legal_name="Retire Corp", commercial_name="Retire Corp", nit="900777888", dv="2")
    db_session.add_all([u, b])
    db_session.commit()

    pdf_bytes = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\nxref\n0 1\ntrailer<</Root 1 0 R>>\n%%EOF"

    # Subir dos documentos
    r1 = client_app.post(
        f"/api/admin/clients/{b.id}/documents",
        headers=admin_headers,
        data={"doc_type": "RUT"},
        files={"file": ("rut.pdf", pdf_bytes, "application/pdf")},
    )
    assert r1.status_code == 201
    doc1_id = r1.json()["id"]

    r2 = client_app.post(
        f"/api/admin/clients/{b.id}/documents",
        headers=admin_headers,
        data={"doc_type": "CAMARA"},
        files={"file": ("camara.pdf", pdf_bytes, "application/pdf")},
    )
    assert r2.status_code == 201
    doc2_id = r2.json()["id"]

    # Listar documentos activos
    r_list = client_app.get(f"/api/admin/clients/{b.id}/documents", headers=admin_headers)
    assert r_list.status_code == 200
    docs = r_list.json()
    assert len(docs) == 2
    # Ordenados de más reciente a más antiguo
    numbers = [d["number"] for d in docs]
    assert 1 in numbers and 2 in numbers

    # Primer retiro -> 204 No Content
    r_del_1 = client_app.delete(f"/api/admin/clients/{b.id}/documents/{doc1_id}", headers=admin_headers)
    assert r_del_1.status_code == 204

    # Segundo retiro del mismo documento -> 409 Conflict
    r_del_2 = client_app.delete(f"/api/admin/clients/{b.id}/documents/{doc1_id}", headers=admin_headers)
    assert r_del_2.status_code == 409
    assert r_del_2.json()["code"] == "ALREADY_RETIRED"

    # Listar nuevamente: solo debe quedar doc2
    r_list_after = client_app.get(f"/api/admin/clients/{b.id}/documents", headers=admin_headers)
    assert r_list_after.status_code == 200
    docs_after = r_list_after.json()
    assert len(docs_after) == 1
    assert docs_after[0]["id"] == doc2_id

    # Documento inexistente -> 404
    r_del_404 = client_app.delete(f"/api/admin/clients/{b.id}/documents/doc-fantasma", headers=admin_headers)
    assert r_del_404.status_code == 404
    assert r_del_404.json()["code"] == "DOCUMENT_NOT_FOUND"


# ==============================================================================
# Escenario 8: Enlace de descarga usado por ADMIN (AC #5)
# ==============================================================================


def test_admin_download_link_used_by_admin(client_app, admin_headers, db_session):
    """POST .../link emite token JWT y GET /api/documents/file/{token} permite la descarga al ADMIN sin suscripción."""
    u = User(id="usr-dl", email="dl@test.co", full_name="User DL", role="CLIENT", is_active=True)
    b = Business(id="biz-dl", client_id=u.id, legal_name="DL Corp", commercial_name="DL Corp", nit="900666555", dv="3")
    db_session.add_all([u, b])
    db_session.commit()

    pdf_bytes = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\nxref\n0 1\ntrailer<</Root 1 0 R>>\n%%EOF"

    # Cargar documento
    r_upload = client_app.post(
        f"/api/admin/clients/{b.id}/documents",
        headers=admin_headers,
        data={"doc_type": "CEDULA"},
        files={"file": ("cedula_rep.pdf", pdf_bytes, "application/pdf")},
    )
    assert r_upload.status_code == 201
    doc_id = r_upload.json()["id"]

    # Generar enlace temporal con sesión de administrador
    r_link = client_app.post(
        f"/api/admin/clients/{b.id}/documents/{doc_id}/link",
        headers=admin_headers,
    )
    assert r_link.status_code == 200
    data_link = r_link.json()
    file_url = data_link["url"]
    assert file_url.startswith("/api/documents/file/")
    assert data_link["expires_in"] == 300

    # Descargar el archivo físicamente usando el enlace generado (sin cabeceras Bearer)
    r_file = client_app.get(file_url)
    assert r_file.status_code == 200
    assert r_file.content == pdf_bytes
    assert r_file.headers["content-type"] == "application/pdf"
    assert "cedula_rep.pdf" in r_file.headers["content-disposition"]
