"""Pruebas de integración para los endpoints de administración de clientes (Story 8.3)."""

from datetime import date, datetime, timedelta
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api.app import app
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import (
    Business,
    BusinessDocument,
    DIANExtractionJob,
    PaymentRecord,
    Subscription,
    TelegramLinkToken,
    User,
)


@pytest.fixture(name="db_session")
def fixture_db_session():
    """Base de datos SQLite en memoria para la suite de administración."""
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
    """Nombre del bot fijo: las pruebas no consultan getMe en Telegram ni dependen del .env."""
    from dian_automation.api import routes_admin

    monkeypatch.setattr(routes_admin, "_resolve_bot_username", lambda: "KontaPruebasBot")


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
        id="usr-admin-katerinn-83",
        email="katerinn.admin@kontable.co",
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
        id="usr-client-andrea-83",
        email="andrea@cliente.co",
        full_name="Andrea Cliente",
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


# ==============================================================================
# Escenario 1: Seguridad — cliente regular -> 403 en todas; sin token -> 401
# ==============================================================================


def test_admin_routes_forbidden_for_clients_and_unauthenticated(client_app, client_headers):
    """Un usuario con rol CLIENT recibe 403 Forbidden en todas las rutas de /api/admin."""
    routes_to_test = [
        ("GET", "/api/admin/clients"),
        ("GET", "/api/admin/clients/biz-123"),
        ("POST", "/api/admin/clients"),
        ("POST", "/api/admin/clients/biz-123/payments"),
        ("PATCH", "/api/admin/clients/biz-123/income-source"),
        ("PATCH", "/api/admin/clients/biz-123/tax-profile"),
        ("POST", "/api/admin/clients/biz-123/activation-link"),
        ("POST", "/api/admin/clients/biz-123/release-telegram"),
    ]

    for method, path in routes_to_test:
        # Con token de cliente regular -> 403
        resp_client = client_app.request(method, path, headers=client_headers, json={})
        assert resp_client.status_code == 403, f"{method} {path} esperado 403, obtuvo {resp_client.status_code}"
        assert resp_client.json()["detail"] == "Acceso solo para la administración."

        # Sin autenticación -> 401
        resp_anon = client_app.request(method, path, json={})
        assert resp_anon.status_code == 401, f"{method} {path} esperado 401, obtuvo {resp_anon.status_code}"


# ==============================================================================
# Escenario 2: Búsqueda por NIT, por celular y por nombre con tilde (AC #1)
# ==============================================================================


def test_search_by_nit_phone_and_name_with_accents(client_app, admin_headers, db_session):
    """Búsqueda por NIT, celular y nombres con tilde (tanto en mayúsculas como sin acentos)."""
    # Sembrar 3 clientes con datos específicos
    u1 = User(
        id="usr-c1",
        email="maria.gomez@cliente.co",
        full_name="María José Gómez",
        phone="3001112233",
        role="CLIENT",
        is_active=True,
    )
    b1 = Business(
        id="biz-c1",
        client_id=u1.id,
        legal_name="María Gómez SAS",
        commercial_name="Gómez Diseños",
        nit="901234567",
        dv="1",
        income_source="DIAN",
        taxpayer_type="PERSONA_JURIDICA",
        is_active=True,
    )
    s1 = Subscription(
        id="sub-c1",
        client_id=u1.id,
        plan="TRIMESTRAL",
        status="ACTIVO",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date.today(),
        cutoff_date=date.today() + timedelta(days=90),
        grace_period_end=date.today() + timedelta(days=93),
    )

    u2 = User(
        id="usr-c2",
        email="hernan.perez@cliente.co",
        full_name="Hernán Pérez",
        phone="3104445566",
        role="CLIENT",
        is_active=True,
    )
    b2 = Business(
        id="biz-c2",
        client_id=u2.id,
        legal_name="Hernán Pérez e Hijos",
        commercial_name="Pérez Repuestos",
        nit="800999888",
        dv="4",
        income_source="MANUAL_SALES",
        taxpayer_type="PERSONA_NATURAL",
        is_active=True,
    )
    s2 = Subscription(
        id="sub-c2",
        client_id=u2.id,
        plan="SEMESTRAL",
        status="EN_MORA",
        discount_rate=Decimal("8.00"),
        base_price=Decimal("300000.00"),
        final_price=Decimal("276000.00"),
        start_date=date.today() - timedelta(days=180),
        cutoff_date=date.today() - timedelta(days=5),
        grace_period_end=date.today() - timedelta(days=2),
    )

    u3 = User(
        id="usr-c3",
        email="alvaro.nustes@cliente.co",
        full_name="Álvaro Ñustes",
        phone="3207778899",
        role="CLIENT",
        is_active=True,
    )
    b3 = Business(
        id="biz-c3",
        client_id=u3.id,
        legal_name="Soluciones Álvaro LTDA",
        commercial_name="Álvaro Soluciones",
        nit="700555444",
        dv="9",
        income_source="DIAN",
        taxpayer_type="PERSONA_JURIDICA",
        is_active=True,
    )
    s3 = Subscription(
        id="sub-c3",
        client_id=u3.id,
        plan="ANUAL",
        status="ACTIVO",
        discount_rate=Decimal("10.00"),
        base_price=Decimal("600000.00"),
        final_price=Decimal("540000.00"),
        start_date=date.today(),
        cutoff_date=date.today() + timedelta(days=365),
        grace_period_end=date.today() + timedelta(days=368),
    )

    db_session.add_all([u1, b1, s1, u2, b2, s2, u3, b3, s3])
    db_session.commit()

    # 1. Búsqueda por NIT
    r_nit = client_app.get("/api/admin/clients?q=901234567", headers=admin_headers)
    assert r_nit.status_code == 200
    data_nit = r_nit.json()
    assert data_nit["total"] == 1
    assert data_nit["items"][0]["nit"] == "901234567"
    assert data_nit["items"][0]["contact_name"] == "María José Gómez"

    # 2. Búsqueda por Celular
    r_phone = client_app.get("/api/admin/clients?q=3104445566", headers=admin_headers)
    assert r_phone.status_code == 200
    data_phone = r_phone.json()
    assert data_phone["total"] == 1
    assert data_phone["items"][0]["phone"] == "3104445566"
    assert data_phone["items"][0]["contact_name"] == "Hernán Pérez"

    # 3. Búsqueda por Nombre con tilde (con tilde y sin tilde)
    r_maria_tilde = client_app.get("/api/admin/clients?q=María", headers=admin_headers)
    assert r_maria_tilde.status_code == 200
    assert r_maria_tilde.json()["total"] == 1
    assert r_maria_tilde.json()["items"][0]["contact_name"] == "María José Gómez"

    r_maria_sin_tilde = client_app.get("/api/admin/clients?q=maria", headers=admin_headers)
    assert r_maria_sin_tilde.status_code == 200
    assert r_maria_sin_tilde.json()["total"] == 1
    assert r_maria_sin_tilde.json()["items"][0]["contact_name"] == "María José Gómez"

    r_perez_tilde = client_app.get("/api/admin/clients?q=Pérez", headers=admin_headers)
    assert r_perez_tilde.status_code == 200
    assert r_perez_tilde.json()["total"] == 1
    assert r_perez_tilde.json()["items"][0]["contact_name"] == "Hernán Pérez"

    r_perez_sin_tilde = client_app.get("/api/admin/clients?q=perez", headers=admin_headers)
    assert r_perez_sin_tilde.status_code == 200
    assert r_perez_sin_tilde.json()["total"] == 1
    assert r_perez_sin_tilde.json()["items"][0]["contact_name"] == "Hernán Pérez"

    r_alvaro_sin_tilde = client_app.get("/api/admin/clients?q=alvaro", headers=admin_headers)
    assert r_alvaro_sin_tilde.status_code == 200
    assert r_alvaro_sin_tilde.json()["total"] == 1
    assert r_alvaro_sin_tilde.json()["items"][0]["contact_name"] == "Álvaro Ñustes"


# ==============================================================================
# Escenario 3: Filtros por income_source y status (AC #1)
# ==============================================================================


def test_list_clients_filters(client_app, admin_headers, db_session):
    """Filtrado de clientes por origen de ingresos (DIAN, MANUAL_SALES) y estado de suscripción."""
    u1 = User(id="usr-f1", email="u1@test.co", full_name="User 1", phone="3000000001", role="CLIENT")
    b1 = Business(id="biz-f1", client_id=u1.id, legal_name="L1", commercial_name="C1", nit="100000001", dv="1", income_source="DIAN")
    s1 = Subscription(id="sub-f1", client_id=u1.id, plan="TRIMESTRAL", status="ACTIVO", base_price=Decimal("150000"), final_price=Decimal("142500"), discount_rate=Decimal("5"), start_date=date.today(), cutoff_date=date.today(), grace_period_end=date.today())

    u2 = User(id="usr-f2", email="u2@test.co", full_name="User 2", phone="3000000002", role="CLIENT")
    b2 = Business(id="biz-f2", client_id=u2.id, legal_name="L2", commercial_name="C2", nit="100000002", dv="2", income_source="MANUAL_SALES")
    s2 = Subscription(id="sub-f2", client_id=u2.id, plan="SEMESTRAL", status="EN_MORA", base_price=Decimal("300000"), final_price=Decimal("276000"), discount_rate=Decimal("8"), start_date=date.today(), cutoff_date=date.today(), grace_period_end=date.today())

    db_session.add_all([u1, b1, s1, u2, b2, s2])
    db_session.commit()

    # Filtro income_source=DIAN
    r_dian = client_app.get("/api/admin/clients?income_source=DIAN", headers=admin_headers)
    assert r_dian.status_code == 200
    assert r_dian.json()["total"] == 1
    assert r_dian.json()["items"][0]["income_source"] == "DIAN"

    # Filtro income_source=MANUAL_SALES
    r_manual = client_app.get("/api/admin/clients?income_source=MANUAL_SALES", headers=admin_headers)
    assert r_manual.status_code == 200
    assert r_manual.json()["total"] == 1
    assert r_manual.json()["items"][0]["income_source"] == "MANUAL_SALES"

    # Filtro status=ACTIVO
    r_activo = client_app.get("/api/admin/clients?status=ACTIVO", headers=admin_headers)
    assert r_activo.status_code == 200
    assert r_activo.json()["total"] == 1
    assert r_activo.json()["items"][0]["subscription_status"] == "ACTIVO"

    # Filtro status=EN_MORA
    r_mora = client_app.get("/api/admin/clients?status=EN_MORA", headers=admin_headers)
    assert r_mora.status_code == 200
    assert r_mora.json()["total"] == 1
    assert r_mora.json()["items"][0]["subscription_status"] == "EN_MORA"


# ==============================================================================
# Escenario 4: Paginación y ordenamiento por nombre comercial (AC #1)
# ==============================================================================


def test_list_clients_pagination_and_ordering(client_app, admin_headers, db_session):
    """Paginación con page_size limitado a 100 y orden alfabético por commercial_name."""
    for i, name in enumerate(["Zapatería Central", "Alfa Tech", "Bazar Colombia"]):
        u = User(id=f"usr-ord-{i}", email=f"user{i}@test.co", full_name=f"Contact {i}", phone=f"30000000{i}", role="CLIENT")
        b = Business(id=f"biz-ord-{i}", client_id=u.id, legal_name=f"Legal {i}", commercial_name=name, nit=f"10000001{i}", dv="0")
        s = Subscription(id=f"sub-ord-{i}", client_id=u.id, plan="TRIMESTRAL", status="ACTIVO", base_price=Decimal("150000"), final_price=Decimal("142500"), discount_rate=Decimal("5"), start_date=date.today(), cutoff_date=date.today(), grace_period_end=date.today())
        db_session.add_all([u, b, s])
    db_session.commit()

    # Página 1 (tamaño 2) -> Esperamos "Alfa Tech", "Bazar Colombia"
    r_p1 = client_app.get("/api/admin/clients?page=1&page_size=2", headers=admin_headers)
    assert r_p1.status_code == 200
    d_p1 = r_p1.json()
    assert d_p1["total"] == 3
    assert d_p1["page"] == 1
    assert d_p1["page_size"] == 2
    assert len(d_p1["items"]) == 2
    assert d_p1["items"][0]["commercial_name"] == "Alfa Tech"
    assert d_p1["items"][1]["commercial_name"] == "Bazar Colombia"

    # Página 2 (tamaño 2) -> Esperamos "Zapatería Central"
    r_p2 = client_app.get("/api/admin/clients?page=2&page_size=2", headers=admin_headers)
    assert r_p2.status_code == 200
    d_p2 = r_p2.json()
    assert len(d_p2["items"]) == 1
    assert d_p2["items"][0]["commercial_name"] == "Zapatería Central"

    # page_size > 100 -> 422
    r_over = client_app.get("/api/admin/clients?page_size=101", headers=admin_headers)
    assert r_over.status_code == 422


# ==============================================================================
# Escenario 5: Ficha completa y 404 (AC #2)
# ==============================================================================


def test_client_detail_ficha_and_404(client_app, admin_headers, db_session):
    """Consulta de la ficha completa con pagos, extracciones, documentos y suscripción."""
    user = User(
        id="usr-ficha-1",
        email="ficha@cliente.co",
        full_name="Carlos Ficha",
        phone="3009998877",
        role="CLIENT",
        is_active=True,
        telegram_chat_id=888777666,
        telegram_username="carlos_ficha",
        is_telegram_linked=True,
    )
    biz = Business(
        id="biz-ficha-1",
        client_id=user.id,
        legal_name="Carlos Ficha SAS",
        commercial_name="Ficha Store",
        nit="901888999",
        dv="5",
        taxpayer_type="PERSONA_JURIDICA",
        legal_rep_doc="1000222333",
        income_source="DIAN",
        iva_periodicity="BIMESTRAL",
        is_withholding_agent=True,
        is_active=True,
    )
    sub = Subscription(
        id="sub-ficha-1",
        client_id=user.id,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date(2026, 1, 1),
        cutoff_date=date(2026, 4, 1),
        grace_period_end=date(2026, 4, 4),
        status="ACTIVO",
    )
    p1 = PaymentRecord(
        id="pay-1",
        subscription_id=sub.id,
        amount=Decimal("142500.00"),
        payment_date=date(2026, 1, 2),
        reference_code="REF-001",
        verified_by_admin_id="usr-admin-katerinn-83",
    )
    job1 = DIANExtractionJob(
        id="job-1",
        business_id=biz.id,
        target_period="2026-01",
        status="SUCCESS",
        attempt_count=1,
    )
    doc1 = BusinessDocument(
        id="doc-1",
        business_id=biz.id,
        doc_type="RUT",
        original_filename="rut.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        storage_key="docs/rut.pdf",
        sha256="abc",
        uploaded_by_user_id=user.id,
    )
    token1 = TelegramLinkToken(
        id="tok-1",
        user_id=user.id,
        token="tok123456",
        expires_at=datetime.utcnow() + timedelta(hours=24),
        is_used=False,
    )
    db_session.add_all([user, biz, sub, p1, job1, doc1, token1])
    db_session.commit()

    # Consulta por UUID
    r = client_app.get(f"/api/admin/clients/{biz.id}", headers=admin_headers)
    assert r.status_code == 200
    data = r.json()

    assert data["business"]["legal_name"] == "Carlos Ficha SAS"
    assert data["business"]["nit"] == "901888999"
    assert data["contact"]["full_name"] == "Carlos Ficha"
    assert data["contact"]["telegram_chat_id"] == 888777666
    assert data["tax_profile"]["iva_periodicity"] == "BIMESTRAL"
    assert data["tax_profile"]["is_withholding_agent"] is True
    assert data["subscription"]["plan"] == "TRIMESTRAL"
    assert data["subscription"]["base_price"] == "150000.00"
    assert len(data["recent_payments"]) == 1
    assert data["recent_payments"][0]["reference_code"] == "REF-001"
    assert len(data["recent_extractions"]) == 1
    assert data["recent_extractions"][0]["status"] == "SUCCESS"
    assert data["active_documents_count"] == 1
    assert data["has_pending_activation_link"] is True

    # 404 si el negocio no existe
    r_404 = client_app.get("/api/admin/clients/biz-inexistente", headers=admin_headers)
    assert r_404.status_code == 404
    assert r_404.json()["code"] == "BUSINESS_NOT_FOUND"


# ==============================================================================
# Escenario 6: Crear persona, empresa y validaciones (NIT duplicado 409) (AC #3)
# ==============================================================================


def test_create_client_persona_and_empresa_and_validations(client_app, admin_headers):
    """Alta de cliente PERSONA y EMPRESA, rechazo 409 por NIT duplicado y 422 por validaciones."""
    # 1. Crear Persona Natural (201)
    payload_persona = {
        "person_type": "PERSONA",
        "contact_name": "Laura Gómez",
        "phone": "3001239988",
        "document_number": "1000123456",
        "plan": "TRIMESTRAL",
    }
    r_persona = client_app.post("/api/admin/clients", headers=admin_headers, json=payload_persona)
    assert r_persona.status_code == 201
    d_persona = r_persona.json()
    assert "business_id" in d_persona
    assert "user_id" in d_persona
    assert "t.me" in d_persona["activation_link"]

    # 2. Crear Empresa (201)
    payload_empresa = {
        "person_type": "EMPRESA",
        "contact_name": "Roberto Gómez",
        "phone": "3104567788",
        "company_name": "Ferretería Central SAS",
        "nit": "901999888",
        "legal_rep_doc": "1000555444",
        "plan": "ANUAL",
        "income_source": "FACTURADOR",
    }
    r_empresa = client_app.post("/api/admin/clients", headers=admin_headers, json=payload_empresa)
    assert r_empresa.status_code == 201
    d_empresa = r_empresa.json()
    assert "business_id" in d_empresa
    assert "user_id" in d_empresa

    # 3. NIT Duplicado -> 409
    payload_dup = {
        "person_type": "EMPRESA",
        "contact_name": "Otro Contacto",
        "phone": "3201112233",
        "company_name": "Otra Empresa SAS",
        "nit": "901999888",  # Mismo NIT
        "legal_rep_doc": "1000999000",
        "plan": "TRIMESTRAL",
    }
    r_dup = client_app.post("/api/admin/clients", headers=admin_headers, json=payload_dup)
    assert r_dup.status_code == 409
    assert r_dup.json()["code"] == "NIT_ALREADY_EXISTS"

    # 4. Validaciones 422:
    # A. Plan inválido
    p_bad_plan = dict(payload_persona, nit="1000777888", document_number=None, plan="INEXISTENTE")
    r_bad_plan = client_app.post("/api/admin/clients", headers=admin_headers, json=p_bad_plan)
    assert r_bad_plan.status_code == 422
    assert r_bad_plan.json()["code"] == "INVALID_PLAN"

    # B. Empresa sin nombre comercial
    p_no_biz_name = dict(payload_empresa, nit="901777666", company_name="")
    r_no_biz_name = client_app.post("/api/admin/clients", headers=admin_headers, json=p_no_biz_name)
    assert r_no_biz_name.status_code == 422
    assert r_no_biz_name.json()["code"] == "MISSING_BUSINESS_NAME"

    # C. Empresa sin cédula de representante
    p_no_rep = dict(payload_empresa, nit="901777666", legal_rep_doc="")
    r_no_rep = client_app.post("/api/admin/clients", headers=admin_headers, json=p_no_rep)
    assert r_no_rep.status_code == 422
    assert r_no_rep.json()["code"] == "MISSING_LEGAL_REP"

    # D. NIT inválido (< 6 dígitos)
    p_bad_nit = dict(payload_persona, document_number="123")
    r_bad_nit = client_app.post("/api/admin/clients", headers=admin_headers, json=p_bad_nit)
    assert r_bad_nit.status_code == 422
    assert r_bad_nit.json()["code"] == "INVALID_NIT"


# ==============================================================================
# Escenario 7: Confirmar pago que reactiva y notifica por Telegram (AC #4)
# ==============================================================================


def test_confirm_payment_reactivates_and_calls_notifier(client_app, admin_headers, db_session, monkeypatch):
    """El registro de pago reactiva la suscripción, guarda verified_by_admin_id y notifica por Telegram."""
    user = User(
        id="usr-pay-1",
        email="pay@cliente.co",
        full_name="Pedro Pagos",
        phone="3005551122",
        role="CLIENT",
        telegram_chat_id=777888999,
        is_telegram_linked=True,
    )
    biz = Business(
        id="biz-pay-1",
        client_id=user.id,
        legal_name="Pedro Pagos SAS",
        commercial_name="Pagos Store",
        nit="901777555",
        dv="2",
    )
    sub = Subscription(
        id="sub-pay-1",
        client_id=user.id,
        plan="TRIMESTRAL",
        status="EN_MORA",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date.today() - timedelta(days=90),
        cutoff_date=date.today() - timedelta(days=5),
        grace_period_end=date.today() - timedelta(days=2),
    )
    db_session.add_all([user, biz, sub])
    db_session.commit()

    # Interceptar notify.send_telegram_message para verificar notificación
    notified_chats = []

    def fake_notifier(chat_id, text, parse_mode="Markdown"):
        notified_chats.append((chat_id, text))
        return True

    monkeypatch.setattr("dian_automation.api.routes_admin.send_telegram_message", fake_notifier)

    payload = {"amount": "142500", "reference": "TR-998811"}
    r = client_app.post(f"/api/admin/clients/{biz.id}/payments", headers=admin_headers, json=payload)
    assert r.status_code == 201
    d = r.json()

    assert d["status"] == "ACTIVO"
    assert d["client_notified"] is True
    assert d["payment"]["amount"] == "142500.00"
    assert d["payment"]["reference_code"] == "TR-998811"
    assert d["payment"]["verified_by_admin_id"] == "usr-admin-katerinn-83"
    assert len(notified_chats) == 1
    assert notified_chats[0][0] == 777888999

    # Verificar que el fallo del notifier no rompe el pago
    def failing_notifier(chat_id, text, parse_mode="Markdown"):
        raise Exception("Telegram timeout")

    monkeypatch.setattr("dian_automation.api.routes_admin.send_telegram_message", failing_notifier)
    r2 = client_app.post(f"/api/admin/clients/{biz.id}/payments", headers=admin_headers, json={"amount": "100000", "reference": "TR-FAIL-NOTIF"})
    assert r2.status_code == 201
    assert r2.json()["client_notified"] is False


# ==============================================================================
# Escenario 8: Cambiar tipo de negocio y perfil tributario (AC #5)
# ==============================================================================


def test_change_income_source_and_tax_profile(client_app, admin_headers, db_session):
    """Cambio entre DIAN y MANUAL_SALES, y ajuste de periodicidad de IVA / retención."""
    user = User(id="usr-tp-1", email="tp@test.co", full_name="User TP", phone="3009990011", role="CLIENT")
    biz = Business(
        id="biz-tp-1",
        client_id=user.id,
        legal_name="TP SAS",
        commercial_name="TP Commercial",
        nit="901333222",
        dv="8",
        income_source="DIAN",
        iva_periodicity="BIMESTRAL",
        is_withholding_agent=False,
    )
    db_session.add_all([user, biz])
    db_session.commit()

    # 1. Cambiar a VENTAS_MANUALES -> responde la ficha actualizada
    r_inc = client_app.patch(
        f"/api/admin/clients/{biz.id}/income-source",
        headers=admin_headers,
        json={"income_source": "VENTAS_MANUALES"},
    )
    assert r_inc.status_code == 200
    assert r_inc.json()["business"]["income_source"] == "MANUAL_SALES"

    # 2. Intentar configurar perfil tributario en negocio de ventas manuales -> 400 NOT_APPLICABLE
    r_tp_fail = client_app.patch(
        f"/api/admin/clients/{biz.id}/tax-profile",
        headers=admin_headers,
        json={"iva_periodicity": "CUATRIMESTRAL", "is_withholding_agent": True},
    )
    assert r_tp_fail.status_code == 400
    assert r_tp_fail.json()["code"] == "NOT_APPLICABLE"

    # 3. Cambiar de nuevo a FACTURADOR
    client_app.patch(
        f"/api/admin/clients/{biz.id}/income-source",
        headers=admin_headers,
        json={"income_source": "FACTURADOR"},
    )

    # 4. Ahora sí configurar perfil tributario exitosamente
    r_tp_ok = client_app.patch(
        f"/api/admin/clients/{biz.id}/tax-profile",
        headers=admin_headers,
        json={"iva_periodicity": "CUATRIMESTRAL", "is_withholding_agent": True},
    )
    assert r_tp_ok.status_code == 200
    d_tp = r_tp_ok.json()
    assert d_tp["tax_profile"]["iva_periodicity"] == "CUATRIMESTRAL"
    assert d_tp["tax_profile"]["is_withholding_agent"] is True


# ==============================================================================
# Escenario 9: Nuevo enlace con Telegram ya vinculado -> 409 (AC #6)
# ==============================================================================


def test_activation_link_linked_conflict_and_generation(client_app, admin_headers, db_session):
    """Si el usuario ya tiene Telegram vinculado -> 409; si no, genera nuevo enlace e invalida previos."""
    # A. Usuario vinculado -> 409
    user_linked = User(
        id="usr-link-1",
        email="linked@test.co",
        full_name="Linked User",
        phone="3001119988",
        role="CLIENT",
        telegram_chat_id=11223344,
        is_telegram_linked=True,
    )
    biz_linked = Business(id="biz-link-1", client_id=user_linked.id, legal_name="L1", commercial_name="C1", nit="901000111", dv="1")
    db_session.add_all([user_linked, biz_linked])
    db_session.commit()

    r_linked = client_app.post(f"/api/admin/clients/{biz_linked.id}/activation-link", headers=admin_headers)
    assert r_linked.status_code == 409
    assert r_linked.json()["code"] == "ALREADY_LINKED"

    # B. Usuario no vinculado -> 200 con enlace
    user_unlinked = User(
        id="usr-link-2",
        email="unlinked@test.co",
        full_name="Unlinked User",
        phone="3001119977",
        role="CLIENT",
        is_telegram_linked=False,
    )
    biz_unlinked = Business(id="biz-link-2", client_id=user_unlinked.id, legal_name="L2", commercial_name="C2", nit="901000222", dv="2")
    # Token previo no usado
    old_tok = TelegramLinkToken(id="old-tok", user_id=user_unlinked.id, token="old123", expires_at=datetime.utcnow() + timedelta(days=1), is_used=False)
    db_session.add_all([user_unlinked, biz_unlinked, old_tok])
    db_session.commit()

    r_ok = client_app.post(f"/api/admin/clients/{biz_unlinked.id}/activation-link", headers=admin_headers)
    assert r_ok.status_code == 200
    assert "t.me" in r_ok.json()["activation_link"]

    # Verificar que el token viejo se invalidó
    db_session.refresh(old_tok)
    assert old_tok.is_used is True


# ==============================================================================
# Escenario 10: Liberar Telegram (AC #7)
# ==============================================================================


def test_release_telegram(client_app, admin_headers, db_session):
    """Desvincula Telegram del dueño del negocio y responde la ficha actualizada."""
    user = User(
        id="usr-rel-1",
        email="release@test.co",
        full_name="Release User",
        phone="3008887766",
        role="CLIENT",
        telegram_chat_id=555666777,
        telegram_username="release_usr",
        is_telegram_linked=True,
    )
    biz = Business(
        id="biz-rel-1",
        client_id=user.id,
        legal_name="Release SAS",
        commercial_name="Release Commercial",
        nit="901444555",
        dv="6",
    )
    db_session.add_all([user, biz])
    db_session.commit()

    r = client_app.post(f"/api/admin/clients/{biz.id}/release-telegram", headers=admin_headers)
    assert r.status_code == 200
    ficha = r.json()

    assert ficha["contact"]["is_telegram_linked"] is False
    assert ficha["contact"]["telegram_chat_id"] is None
    assert ficha["contact"]["telegram_username"] is None

    # Ahora sí se puede generar un nuevo enlace de activación sin conflicto 409
    r_link = client_app.post(f"/api/admin/clients/{biz.id}/activation-link", headers=admin_headers)
    assert r_link.status_code == 200
    assert "t.me" in r_link.json()["activation_link"]


def test_activation_link_without_bot_username_is_503(client_app, admin_headers, regular_client_user, db_session, monkeypatch):
    """Sin el @username real del bot no se arma un enlace t.me con un nombre supuesto (podría ser de un tercero)."""
    from dian_automation.api import routes_admin

    biz = Business(
        id="biz-sin-bot", client_id=regular_client_user.id, legal_name="Sin Bot SAS", commercial_name="Sin Bot",
        nit="900111222", dv="1", income_source="DIAN", taxpayer_type="PERSONA_JURIDICA", is_active=True,
    )
    regular_client_user.is_telegram_linked = False
    regular_client_user.telegram_chat_id = None
    db_session.add(biz)
    db_session.commit()
    monkeypatch.setattr(routes_admin, "_resolve_bot_username", lambda: None)

    resp = client_app.post("/api/admin/clients/biz-sin-bot/activation-link", headers=admin_headers)

    assert resp.status_code == 503
    assert resp.json()["code"] == "BOT_USERNAME_UNAVAILABLE"
