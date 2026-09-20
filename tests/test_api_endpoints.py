"""Pruebas unitarias y de integración para los Endpoints FastAPI de Kontable."""

import time
import pytest
from datetime import date, datetime, timedelta
from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.db.database import Base, get_db
from dian_automation.db.models import User, Business, Invoice, MonthlyTaxSummary, Subscription
from dian_automation.api.app import app


@pytest.fixture(name="db_session")
def fixture_db_session():
    """Base de datos en memoria compartida entre hilos para pruebas de la API."""
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(name="client")
def fixture_client(db_session, bearer):
    """TestClient con override de get_db y el Bearer del dueño sembrado (usr-andrea-001)."""
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app, headers=bearer("usr-andrea-001"))
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(name="seed_data")
def fixture_seed_data(db_session):
    """Genera usuario, negocio, facturas, resúmenes y suscripción para pruebas."""
    user = User(
        id="usr-andrea-001",
        email="andrea@torres.com",
        full_name="Andrea Torres",
        phone="3001234567",
        role="CLIENT",
        is_active=True,
    )
    db_session.add(user)

    biz = Business(
        id="biz-andrea-diseno",
        client_id=user.id,
        legal_name="Andrea Torres Diseño SAS",
        commercial_name="Andrea Torres Diseño",
        nit="901234567",
        dv="1",
        economic_activity="Servicios de diseño",
        taxpayer_type="PERSONA_JURIDICA",
        is_active=True,
    )
    db_session.add(biz)

    # Suscripción activa
    sub = Subscription(
        id="sub-andrea-001",
        client_id=user.id,
        plan="TRIMESTRAL",
        start_date=date.today() - timedelta(days=30),
        cutoff_date=date.today() + timedelta(days=60),
        grace_period_end=date.today() + timedelta(days=63),
        status="ACTIVO",
        base_price=Decimal("150000.00"),
        discount_rate=Decimal("5.00"),
        final_price=Decimal("142500.00"),
    )
    db_session.add(sub)

    # Resúmenes mensuales de impuestos (últimos 2 meses)
    summary_ago = MonthlyTaxSummary(
        business_id=biz.id,
        period_year_month="2026-08",
        total_invoiced_net=Decimal("12450000.00"),
        iva_generado=Decimal("2365500.00"),
        iva_descontable=Decimal("540000.00"),
        iva_balance=Decimal("1825500.00"),
        total_invoices_count=18,
        variation_vs_previous_pct=Decimal("8.00"),
    )
    summary_jul = MonthlyTaxSummary(
        business_id=biz.id,
        period_year_month="2026-07",
        total_invoiced_net=Decimal("11700000.00"),
        iva_generado=Decimal("2223000.00"),
        iva_descontable=Decimal("490000.00"),
        iva_balance=Decimal("1733000.00"),
        total_invoices_count=16,
        variation_vs_previous_pct=Decimal("19.38"),
    )
    db_session.add_all([summary_ago, summary_jul])

    # Facturas emitidas y recibidas
    inv1 = Invoice(
        id="inv-001",
        business_id=biz.id,
        cufe="cufe-001-mock",
        document_type="Factura electrónica de venta",
        prefix="FE",
        folio="1042",
        issue_date=datetime(2026, 8, 22, 10, 30),
        issuer_nit="901234567",
        issuer_name="Andrea Torres Diseño",
        receiver_nit="800999111",
        receiver_name="Estudio Craft SAS",
        iva=Decimal("351500.00"),
        total=Decimal("1850000.00"),
        group_type="Emitido",
    )
    inv2 = Invoice(
        id="inv-002",
        business_id=biz.id,
        cufe="cufe-002-mock",
        document_type="Factura electrónica de venta",
        prefix="FE",
        folio="1041",
        issue_date=datetime(2026, 8, 19, 14, 0),
        issuer_nit="901234567",
        issuer_name="Andrea Torres Diseño",
        receiver_nit="900111222",
        receiver_name="Marca Río",
        iva=Decimal("456000.00"),
        total=Decimal("2400000.00"),
        group_type="Emitido",
    )
    inv3 = Invoice(
        id="inv-003",
        business_id=biz.id,
        cufe="cufe-003-mock",
        document_type="Factura electrónica de venta",
        prefix="REC",
        folio="550",
        issue_date=datetime(2026, 8, 15, 9, 15),
        issuer_nit="890123456",
        issuer_name="Papelería Andina SAS",
        receiver_nit="901234567",
        receiver_name="Andrea Torres Diseño",
        iva=Decimal("190000.00"),
        total=Decimal("1000000.00"),
        group_type="Recibido",
    )
    # Negocio adicional con origen de ingresos MANUAL_SALES para Story 6.3
    biz_manual = Business(
        id="biz-andrea-manual",
        client_id=user.id,
        legal_name="Andrea Torres Manual SAS",
        commercial_name="Andrea Manual",
        nit="901999888",
        dv="2",
        economic_activity="Comercio al por menor",
        taxpayer_type="PERSONA_NATURAL",
        income_source="MANUAL_SALES",
        is_active=True,
    )
    inv_manual = Invoice(
        id="inv-manual-001",
        business_id=biz_manual.id,
        cufe="cufe-manual-001",
        document_type="Factura electrónica de venta",
        prefix="REC",
        folio="101",
        issue_date=datetime(2026, 8, 10, 10, 0),
        issuer_nit="800111222",
        issuer_name="Proveedor Papelería",
        receiver_nit="901999888",
        receiver_name="Andrea Torres Manual SAS",
        iva=Decimal("19000.00"),
        total=Decimal("119000.00"),
        group_type="Recibido",
    )
    db_session.add_all([inv1, inv2, inv3, biz_manual, inv_manual])
    db_session.commit()

    return {"user": user, "business": biz, "biz_manual": biz_manual, "sub": sub}


def test_root_and_health_endpoints(client):
    """Verifica que la API raíz y health check respondan 200 OK."""
    r_root = client.get("/")
    assert r_root.status_code == 200
    assert r_root.json()["app"] == "Kontable API"

    r_health = client.get("/health")
    assert r_health.status_code == 200
    assert r_health.json()["status"] == "ok"


def test_dashboard_endpoint_success(client, seed_data):
    """Valida que el Dashboard entregue el payload estructurado con tiempos < 100ms."""
    biz_id = seed_data["business"].id

    t_start = time.perf_counter()
    response = client.get(f"/api/dashboard/{biz_id}")
    t_elapsed = (time.perf_counter() - t_start) * 1000  # ms

    assert response.status_code == 200
    assert t_elapsed < 100, f"Tiempo de respuesta {t_elapsed:.2f}ms excede los 100ms"

    data = response.json()
    # 1. Business
    assert data["business"]["commercial_name"] == "Andrea Torres Diseño"
    assert data["business"]["nit"] == "901234567"

    # 2. Resumen
    assert data["resumen"]["total"] == 12450000.0
    assert data["resumen"]["ivaAcumulado"] == 2365500.0
    assert data["resumen"]["variacion"] == 8.0

    # 3. Histórico
    assert len(data["historico"]) == 2
    assert data["historico"][-1]["total"] == 12450000.0

    # 4. Facturas Recientes
    assert len(data["facturasRecientes"]) == 2
    assert data["facturasRecientes"][0]["cliente"] == "Estudio Craft SAS"
    assert data["facturasRecientes"][0]["valor"] == 1850000.0

    # 5. Suscripción
    assert data["suscripcion"]["estado"] == "ACTIVO"
    assert data["suscripcion"]["has_warning_banner"] is False


def test_dashboard_by_nit(client, seed_data):
    """Permite consultar el dashboard usando directamente el NIT del negocio."""
    nit = seed_data["business"].nit
    response = client.get(f"/api/dashboard/{nit}")
    assert response.status_code == 200
    assert response.json()["business"]["id"] == seed_data["business"].id


def test_dashboard_business_not_found(client, seed_data):
    """Valida error 404 si el negocio no existe."""
    response = client.get("/api/dashboard/biz-inexistente")
    assert response.status_code == 404
    assert "No se encontró el negocio" in response.json()["detail"]


def test_iva_detail_endpoint(client, seed_data):
    """Valida la consulta de periodos de IVA con cálculo del Ring SVG."""
    biz_id = seed_data["business"].id

    t_start = time.perf_counter()
    response = client.get(f"/api/iva/{biz_id}")
    t_elapsed = (time.perf_counter() - t_start) * 1000

    assert response.status_code == 200
    assert t_elapsed < 100

    data = response.json()
    assert data["nit"] == "901234567"
    assert len(data["periodos"]) >= 1

    latest_period = data["periodos"][0]
    assert latest_period["generado"] == 2365500.0
    assert latest_period["descontable"] == 540000.0
    assert latest_period["saldo"] == 1825500.0
    # pct = descontable / generado
    assert 0.0 < latest_period["pct"] <= 1.0


def test_invoices_list_and_filters(client, seed_data):
    """Valida el historial de facturas con filtros y búsqueda."""
    biz_id = seed_data["business"].id

    # 1. Sin filtros: total 3 facturas
    r_all = client.get(f"/api/invoices/{biz_id}")
    assert r_all.status_code == 200
    assert r_all.json()["total_count"] == 3

    # 2. Filtrar por emitidos: 2 facturas
    r_emit = client.get(f"/api/invoices/{biz_id}?group_type=Emitido")
    assert r_emit.status_code == 200
    assert r_emit.json()["total_count"] == 2
    assert all(inv["group_type"] == "Emitido" for inv in r_emit.json()["invoices"])

    # 3. Filtrar por recibidos: 1 factura
    r_rec = client.get(f"/api/invoices/{biz_id}?group_type=Recibido")
    assert r_rec.status_code == 200
    assert r_rec.json()["total_count"] == 1
    assert r_rec.json()["invoices"][0]["cliente"] == "Papelería Andina SAS"

    # 4. Búsqueda por texto
    r_search = client.get(f"/api/invoices/{biz_id}?search=Craft")
    assert r_search.status_code == 200
    assert r_search.json()["total_count"] == 1
    assert "Craft" in r_search.json()["invoices"][0]["cliente"]


def test_grace_period_warning_banner(client, db_session, seed_data):
    """Verifica que si la suscripción está EN_MORA pero en gracia, retorna 200 con banner."""
    sub = seed_data["sub"]
    sub.status = "EN_MORA"
    sub.cutoff_date = date.today() - timedelta(days=1)
    sub.grace_period_end = date.today() + timedelta(days=2)
    db_session.commit()

    response = client.get(f"/api/dashboard/{seed_data['business'].id}")
    assert response.status_code == 200
    data = response.json()
    assert data["suscripcion"]["has_warning_banner"] is True
    assert data["suscripcion"]["days_left_in_grace"] == 2


def test_lockout_returns_403_forbidden_on_all_endpoints(client, db_session, seed_data):
    """Verifica que cuando la suscripción está BLOQUEADA, todos los endpoints retornen 403."""
    sub = seed_data["sub"]
    sub.status = "BLOQUEADO"
    db_session.commit()

    biz_id = seed_data["business"].id

    # 1. Dashboard
    r_dash = client.get(f"/api/dashboard/{biz_id}")
    assert r_dash.status_code == 403
    d_dash = r_dash.json()
    assert d_dash["error"] == "SUBSCRIPTION_BLOCKED"
    assert d_dash["redirect_url"] == "/servicio-suspendido"
    assert "suspendida temporalmente" in d_dash["message"]

    # 2. IVA
    r_iva = client.get(f"/api/iva/{biz_id}")
    assert r_iva.status_code == 403
    assert r_iva.json()["error"] == "SUBSCRIPTION_BLOCKED"

    # 3. Invoices
    r_inv = client.get(f"/api/invoices/{biz_id}")
    assert r_inv.status_code == 403
    assert r_inv.json()["error"] == "SUBSCRIPTION_BLOCKED"


def test_frontend_static_files_served(client):
    """El frontend web/móvil (ProyectoDianFront) queda montado en /app para el enlace de Telegram."""
    r = client.get("/app/kontable-prototipo_1.html")
    assert r.status_code == 200
    assert "Kontable" in r.text


def test_public_config_resolves_real_bot_username(client, monkeypatch):
    """El front nunca debe adivinar el @username del bot: /api/config lo resuelve contra Telegram
    usando el TELEGRAM_BOT_TOKEN real de .env (getMe), y nunca expone el token."""
    import dian_automation.api.routes_config as config_module

    monkeypatch.setattr(config_module, "_cached_bot_username", None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake-token-for-test")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"result": {"username": "MiNegocioKontableBot", "first_name": "Kontable"}}

    def fake_get(url, timeout=5.0):
        assert "123456:fake-token-for-test" in url
        return FakeResponse()

    monkeypatch.setattr(config_module.httpx, "get", fake_get)

    res = client.get("/api/config")
    assert res.status_code == 200
    body = res.json()
    assert body["telegram_bot_username"] == "MiNegocioKontableBot"
    assert "fake-token-for-test" not in str(body)


def test_public_config_without_token_returns_none(client, monkeypatch):
    """Sin TELEGRAM_BOT_TOKEN configurado, /api/config no debe inventar ningún @username."""
    import dian_automation.api.routes_config as config_module

    monkeypatch.setattr(config_module, "_cached_bot_username", None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CLIENT_BOT_TOKEN", raising=False)

    res = client.get("/api/config")
    assert res.status_code == 200
    assert res.json()["telegram_bot_username"] is None


def test_iva_and_dashboard_return_404_for_manual_sales_business(client, seed_data):
    """GET /api/iva/{id} y GET /api/dashboard/{id} devuelven 404 para un negocio MANUAL_SALES."""
    r_iva = client.get("/api/iva/biz-andrea-manual")
    assert r_iva.status_code == 404
    assert r_iva.json()["detail"] == "Este servicio no aplica a tu tipo de negocio."

    r_dash = client.get("/api/dashboard/biz-andrea-manual")
    assert r_dash.status_code == 404
    assert r_dash.json()["detail"] == "Este servicio no aplica a tu tipo de negocio."


def test_invoices_returns_200_for_manual_sales_business(client, seed_data):
    """GET /api/invoices/{id} sigue respondiendo 200 para un negocio MANUAL_SALES."""
    r_inv = client.get("/api/invoices/biz-andrea-manual")
    assert r_inv.status_code == 200
    data = r_inv.json()
    assert "invoices" in data
    assert len(data["invoices"]) == 1
    assert data["invoices"][0]["id"] == "inv-manual-001"


def test_dian_business_retains_iva_and_dashboard(client, seed_data):
    """Para negocio DIAN, /api/iva y /api/dashboard siguen respondiendo 200."""
    r_iva = client.get("/api/iva/biz-andrea-diseno")
    assert r_iva.status_code == 200

    r_dash = client.get("/api/dashboard/biz-andrea-diseno")
    assert r_dash.status_code == 200

