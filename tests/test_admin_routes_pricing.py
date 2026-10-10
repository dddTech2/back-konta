"""Pruebas de integración para Story 9.2: API de administración de tarifas, planes y precios."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api.app import app
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import (
    BillingSetting,
    Business,
    PaymentRecord,
    PricingPlan,
    PricingRate,
    Subscription,
    SubscriptionPriceChange,
    User,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    PRICE_ORIGIN_ESPECIAL,
    PRICE_ORIGIN_TARIFA,
    TAXPAYER_TYPE_PERSONA_JURIDICA,
    TAXPAYER_TYPE_PERSONA_NATURAL,
)
from dian_automation.subscriptions.pricing import ensure_default_pricing


@pytest.fixture(name="db_session")
def fixture_db_session():
    """Base de datos SQLite en memoria con esquema creado."""
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
    """Nombre del bot mockeado para evitar consultas de red."""
    from dian_automation.api import routes_admin

    monkeypatch.setattr(routes_admin, "_resolve_bot_username", lambda: "KontaPruebasBot")


@pytest.fixture(name="client_app")
def fixture_client_app(db_session):
    """TestClient configurado con sesión db_session."""
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
    """Administrador autenticado."""
    admin = User(
        id="usr-admin-katerinn-92",
        email="katerinn.pricing@kontable.co",
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
        id="usr-client-andrea-92",
        email="andrea.client@kontable.co",
        full_name="Andrea Cliente",
        role="CLIENT",
        is_active=True,
    )
    db_session.add(client)
    db_session.commit()
    return client


@pytest.fixture
def admin_headers(bearer, admin_user):
    """Cabeceras Bearer de admin."""
    return bearer(admin_user.id)


@pytest.fixture
def client_headers(bearer, regular_client_user):
    """Cabeceras Bearer de cliente."""
    return bearer(regular_client_user.id)


# ==============================================================================
# 1. Seguridad: 401 sin token y 403 para cliente no admin
# ==============================================================================


def test_pricing_routes_security(client_app, client_headers):
    """Verifica que todas las rutas bajo /api/admin/pricing requieran admin."""
    endpoints = [
        ("GET", "/api/admin/pricing", None),
        ("PUT", "/api/admin/pricing/plans/TRIMESTRAL", {"name": "Trimestral VIP"}),
        ("POST", "/api/admin/pricing/plans", {"code": "TEST", "name": "Test", "months": 3, "discount_rate": "5.00"}),
        ("GET", "/api/admin/pricing/settings", None),
        ("PUT", "/api/admin/pricing/settings", {"grace_days": 5}),
        ("POST", "/api/admin/pricing/rates", {"income_source": "DIAN", "taxpayer_type": "PERSONA_NATURAL", "monthly_price": "55000"}),
        ("DELETE", "/api/admin/pricing/rates/rate-123", None),
        ("GET", "/api/admin/pricing/rates/history?income_source=DIAN&taxpayer_type=PERSONA_NATURAL", None),
        ("GET", "/api/admin/pricing/quote?income_source=DIAN&taxpayer_type=PERSONA_NATURAL&plan=TRIMESTRAL", None),
        ("PATCH", "/api/admin/clients/biz-123/subscription", {"plan": "ANUAL", "reason": "Upgrade"}),
    ]

    for method, path, body in endpoints:
        # Sin token -> 401
        resp_anon = client_app.request(method, path, json=body)
        assert resp_anon.status_code == 401, f"{method} {path} esperado 401, obtuvo {resp_anon.status_code}"

        # Cliente no admin -> 403
        resp_cli = client_app.request(method, path, headers=client_headers, json=body)
        assert resp_cli.status_code == 403, f"{method} {path} esperado 403, obtuvo {resp_cli.status_code}"


# ==============================================================================
# 2. GET /api/admin/pricing (AC #1)
# ==============================================================================


def test_get_pricing_overview_seed(client_app, admin_headers):
    """GET /api/admin/pricing devuelve 4 tarifas vigentes, 12 cotizaciones, grace_days 3 y planes ordenados."""
    res = client_app.get("/api/admin/pricing", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()

    assert data["grace_days"] == 3
    assert len(data["plans"]) == 4
    plan_codes = [p["code"] for p in data["plans"]]
    assert plan_codes == ["MENSUAL", "TRIMESTRAL", "SEMESTRAL", "ANUAL"]
    assert data["plans"][0]["is_active"] is False
    assert data["plans"][1]["is_active"] is True

    # 4 tarifas de los 4 segmentos en orden estricto
    rates = data["rates"]
    assert len(rates) == 4
    assert rates[0]["income_source"] == "DIAN" and rates[0]["taxpayer_type"] == "PERSONA_NATURAL"
    assert rates[1]["income_source"] == "DIAN" and rates[1]["taxpayer_type"] == "PERSONA_JURIDICA"
    assert rates[2]["income_source"] == "MANUAL_SALES" and rates[2]["taxpayer_type"] == "PERSONA_NATURAL"
    assert rates[3]["income_source"] == "MANUAL_SALES" and rates[3]["taxpayer_type"] == "PERSONA_JURIDICA"
    for r in rates:
        assert r["monthly_price"] == "50000.00"

    # scheduled vacío inicialmente
    assert data["scheduled"] == []

    # quotes: 4 segmentos x 3 planes activos = 12 cotizaciones
    quotes = data["quotes"]
    assert len(quotes) == 12
    for q in quotes:
        assert "monthly_price" in q
        assert "gross_total" in q
        assert "discount_rate" in q
        assert "discount_amount" in q
        assert "final_price" in q
        assert "." in q["final_price"]


# ==============================================================================
# 3. PUT /api/admin/pricing/plans/{code} (AC #2)
# ==============================================================================


def test_update_plan(client_app, admin_headers):
    """Actualización de plan y sus validaciones (422, 404, 409)."""
    # Éxito actualizando nombre y descuento
    res = client_app.put(
        "/api/admin/pricing/plans/TRIMESTRAL",
        headers=admin_headers,
        json={"name": "Trimestral Pro", "discount_rate": "6.50"},
    )
    assert res.status_code == 200
    d = res.json()
    assert d["code"] == "TRIMESTRAL"
    assert d["name"] == "Trimestral Pro"
    assert d["discount_rate"] == "6.50"

    # Enviar months -> 422 con mensaje exacto
    res_months = client_app.put(
        "/api/admin/pricing/plans/TRIMESTRAL",
        headers=admin_headers,
        json={"months": 4},
    )
    assert res_months.status_code == 422
    assert "La duración de un plan no se cambia" in res_months.json()["detail"]

    # Descuento fuera de rango -> 422
    res_disc = client_app.put(
        "/api/admin/pricing/plans/TRIMESTRAL",
        headers=admin_headers,
        json={"discount_rate": "55.00"},
    )
    assert res_disc.status_code == 422

    # Plan no encontrado -> 404 PLAN_NOT_FOUND
    res_404 = client_app.put(
        "/api/admin/pricing/plans/INEXISTENTE",
        headers=admin_headers,
        json={"name": "Nuevo"},
    )
    assert res_404.status_code == 404
    assert res_404.json()["code"] == "PLAN_NOT_FOUND"

    # Desactivar todos los planes excepto uno, e intentar desactivar el último -> 409 LAST_ACTIVE_PLAN
    client_app.put("/api/admin/pricing/plans/SEMESTRAL", headers=admin_headers, json={"is_active": False})
    client_app.put("/api/admin/pricing/plans/ANUAL", headers=admin_headers, json={"is_active": False})
    res_last = client_app.put("/api/admin/pricing/plans/TRIMESTRAL", headers=admin_headers, json={"is_active": False})
    assert res_last.status_code == 409
    assert res_last.json()["code"] == "LAST_ACTIVE_PLAN"


# ==============================================================================
# 4. POST /api/admin/pricing/plans (AC #2b)
# ==============================================================================


def test_create_plan_and_appears_in_quotes(client_app, admin_headers):
    """Creación de plan 201 y validaciones de formato (422) y duplicado (409)."""
    # 201 plan nuevo
    res = client_app.post(
        "/api/admin/pricing/plans",
        headers=admin_headers,
        json={"code": "BIANUAL", "name": "Plan Bianual", "months": 24, "discount_rate": "15.00"},
    )
    assert res.status_code == 201
    plan_data = res.json()
    assert plan_data["code"] == "BIANUAL"
    assert plan_data["months"] == 24
    assert plan_data["discount_rate"] == "15.00"
    assert plan_data["is_active"] is True

    # Aparece en cotizaciones de GET /api/admin/pricing
    res_overview = client_app.get("/api/admin/pricing", headers=admin_headers)
    quotes = res_overview.json()["quotes"]
    bianual_quotes = [q for q in quotes if q["plan"] == "BIANUAL"]
    assert len(bianual_quotes) == 4

    # Duplicado -> 409 PLAN_EXISTS
    res_dup = client_app.post(
        "/api/admin/pricing/plans",
        headers=admin_headers,
        json={"code": "BIANUAL", "name": "Otro Bianual", "months": 24, "discount_rate": "15.00"},
    )
    assert res_dup.status_code == 409
    assert res_dup.json()["code"] == "PLAN_EXISTS"

    # Validaciones de formato -> 422
    # Código en minúscula o inválido
    assert client_app.post(
        "/api/admin/pricing/plans",
        headers=admin_headers,
        json={"code": "BAD-CODE", "name": "Plan", "months": 12, "discount_rate": "10.00"},
    ).status_code == 422

    # Meses fuera de 1-36
    assert client_app.post(
        "/api/admin/pricing/plans",
        headers=admin_headers,
        json={"code": "TRIENAL", "name": "Trienal", "months": 48, "discount_rate": "20.00"},
    ).status_code == 422


# ==============================================================================
# 5. Configuración de Días de Gracia (AC #2c)
# ==============================================================================


def test_billing_settings_and_client_creation_uses_new_grace_days(client_app, admin_headers):
    """GET/PUT settings de gracia, rango 1–15 y efecto en clientes creados."""
    res_get = client_app.get("/api/admin/pricing/settings", headers=admin_headers)
    assert res_get.status_code == 200
    assert res_get.json()["grace_days"] == 3

    # Fuera de rango -> 422
    assert client_app.put(
        "/api/admin/pricing/settings",
        headers=admin_headers,
        json={"grace_days": 0},
    ).status_code == 422
    assert client_app.put(
        "/api/admin/pricing/settings",
        headers=admin_headers,
        json={"grace_days": 16},
    ).status_code == 422

    # Actualizar a 7 días
    res_put = client_app.put(
        "/api/admin/pricing/settings",
        headers=admin_headers,
        json={"grace_days": 7},
    )
    assert res_put.status_code == 200
    assert res_put.json()["grace_days"] == 7

    # Crear cliente después y verificar que cutoff_date + 7 == grace_period_end
    res_cli = client_app.post(
        "/api/admin/clients",
        headers=admin_headers,
        json={
            "person_type": "PERSONA",
            "contact_name": "Cliente Gracia",
            "phone": "3001112233",
            "document_number": "1000999888",
            "plan": "TRIMESTRAL",
        },
    )
    assert res_cli.status_code == 201
    cli_data = res_cli.json()
    biz_id = cli_data["business_id"]

    # Ficha del cliente
    ficha = client_app.get(f"/api/admin/clients/{biz_id}", headers=admin_headers).json()
    sub = ficha["subscription"]
    cutoff = date.fromisoformat(sub["cutoff_date"])
    grace_end = date.fromisoformat(sub["grace_period_end"])
    assert (grace_end - cutoff).days == 7


# ==============================================================================
# 6. Tarifas: POST hoy/futura/pasada/duplicada (AC #3)
# ==============================================================================


def test_rates_creation_and_update(client_app, admin_headers, db_session):
    """POST /api/admin/pricing/rates para hoy (200 si actualiza), futura (201), pasada (422)."""
    today_bogota = datetime.now(ZoneInfo("America/Bogota")).date()

    # Primera tarifa con fecha de hoy (la semilla rige desde 2026-01-01) -> 201
    res_create_today = client_app.post(
        "/api/admin/pricing/rates",
        headers=admin_headers,
        json={
            "income_source": "DIAN",
            "taxpayer_type": "PERSONA_NATURAL",
            "monthly_price": "55000.00",
            "effective_from": today_bogota.isoformat(),
        },
    )
    assert res_create_today.status_code == 201

    # Misma fecha otra vez: se actualiza la fila en lugar de duplicarla -> 200
    res_update_today = client_app.post(
        "/api/admin/pricing/rates",
        headers=admin_headers,
        json={
            "income_source": "DIAN",
            "taxpayer_type": "PERSONA_NATURAL",
            "monthly_price": "60000.00",
            "effective_from": today_bogota.isoformat(),
        },
    )
    assert res_update_today.status_code == 200
    assert res_update_today.json()["monthly_price"] == "60000.00"

    # Crear tarifa futura -> 201
    future_date = today_bogota + timedelta(days=30)
    res_future = client_app.post(
        "/api/admin/pricing/rates",
        headers=admin_headers,
        json={
            "income_source": "DIAN",
            "taxpayer_type": "PERSONA_NATURAL",
            "monthly_price": "70000.00",
            "effective_from": future_date.isoformat(),
        },
    )
    assert res_future.status_code == 201
    fut_id = res_future.json()["id"]

    # Aparece en scheduled
    res_overview = client_app.get("/api/admin/pricing", headers=admin_headers)
    scheduled = res_overview.json()["scheduled"]
    assert any(s["id"] == fut_id for s in scheduled)

    # Tarifa pasada -> 422
    past_date = today_bogota - timedelta(days=5)
    res_past = client_app.post(
        "/api/admin/pricing/rates",
        headers=admin_headers,
        json={
            "income_source": "DIAN",
            "taxpayer_type": "PERSONA_NATURAL",
            "monthly_price": "40000.00",
            "effective_from": past_date.isoformat(),
        },
    )
    assert res_past.status_code == 422
    assert res_past.json()["code"] == "INVALID_EFFECTIVE_DATE"

    # Monto <= 0 o > 10.000.000 -> 422
    assert client_app.post(
        "/api/admin/pricing/rates",
        headers=admin_headers,
        json={"income_source": "DIAN", "taxpayer_type": "PERSONA_NATURAL", "monthly_price": "0"},
    ).status_code == 422
    assert client_app.post(
        "/api/admin/pricing/rates",
        headers=admin_headers,
        json={"income_source": "DIAN", "taxpayer_type": "PERSONA_NATURAL", "monthly_price": "15000000"},
    ).status_code == 422


# ==============================================================================
# 7. DELETE /api/admin/pricing/rates/{id} (AC #4)
# ==============================================================================


def test_delete_scheduled_rate_vs_effective(client_app, admin_headers, db_session):
    """Eliminar tarifa programada (204) vs tarifa vigente hoy (409 RATE_ALREADY_EFFECTIVE)."""
    today_bogota = datetime.now(ZoneInfo("America/Bogota")).date()
    future_date = today_bogota + timedelta(days=15)

    # Crear programada
    res_fut = client_app.post(
        "/api/admin/pricing/rates",
        headers=admin_headers,
        json={
            "income_source": "MANUAL_SALES",
            "taxpayer_type": "PERSONA_JURIDICA",
            "monthly_price": "80000.00",
            "effective_from": future_date.isoformat(),
        },
    )
    rate_id = res_fut.json()["id"]

    # 204 DELETE tarifa programada
    res_del = client_app.delete(f"/api/admin/pricing/rates/{rate_id}", headers=admin_headers)
    assert res_del.status_code == 204

    # 404 al intentar borrar de nuevo
    assert client_app.delete(f"/api/admin/pricing/rates/{rate_id}", headers=admin_headers).status_code == 404

    # Tarifa vigente de la semilla -> 409 RATE_ALREADY_EFFECTIVE
    active_rate = db_session.query(PricingRate).filter(PricingRate.effective_from <= today_bogota).first()
    res_cant_del = client_app.delete(f"/api/admin/pricing/rates/{active_rate.id}", headers=admin_headers)
    assert res_cant_del.status_code == 409
    assert res_cant_del.json()["code"] == "RATE_ALREADY_EFFECTIVE"


# ==============================================================================
# 8. Historial y Cotizaciones (AC #5 y AC #6)
# ==============================================================================


def test_rate_history_and_quote_with_person_type(client_app, admin_headers):
    """GET /rates/history y GET /quote con person_type y validaciones."""
    # Historial
    res_hist = client_app.get(
        "/api/admin/pricing/rates/history?income_source=DIAN&taxpayer_type=PERSONA_NATURAL",
        headers=admin_headers,
    )
    assert res_hist.status_code == 200
    assert len(res_hist.json()) >= 1

    # Quote con person_type=PERSONA y FACTURADOR
    res_quote = client_app.get(
        "/api/admin/pricing/quote?income_source=FACTURADOR&person_type=PERSONA&plan=TRIMESTRAL",
        headers=admin_headers,
    )
    assert res_quote.status_code == 200
    q = res_quote.json()
    assert q["income_source"] == "DIAN"
    assert q["taxpayer_type"] == "PERSONA_NATURAL"
    assert q["plan"] == "TRIMESTRAL"
    assert "start_date" in q
    assert "cutoff_date" in q
    assert q["gross_total"] == "150000.00"
    assert q["final_price"] == "142500.00"

    # Plan inactivo (MENSUAL) -> 400 INVALID_PLAN
    res_inactive = client_app.get(
        "/api/admin/pricing/quote?income_source=DIAN&person_type=PERSONA&plan=MENSUAL",
        headers=admin_headers,
    )
    assert res_inactive.status_code == 400
    assert res_inactive.json()["code"] == "INVALID_PLAN"


# ==============================================================================
# 9. Ficha y PATCH /api/admin/clients/{business_id}/subscription (AC #7 y AC #8)
# ==============================================================================


def test_patch_client_subscription_and_ficha_details(client_app, admin_headers, db_session):
    """Pruebas de cambio especial, plan, volver a tarifa y registro en la ficha."""
    # 1. Crear un cliente
    res_create = client_app.post(
        "/api/admin/clients",
        headers=admin_headers,
        json={
            "person_type": "PERSONA",
            "contact_name": "Mario Pruebas",
            "phone": "3008889900",
            "document_number": "1000555666",
            "plan": "TRIMESTRAL",
        },
    )
    assert res_create.status_code == 201
    c_data = res_create.json()
    biz_id = c_data["business_id"]

    # AC #9: Verificar campos de respuesta al crear cliente
    assert c_data["final_price"] == "142500.00"
    assert c_data["monthly_price"] == "50000.00"
    assert c_data["discount_rate"] == "5.00"
    assert "cutoff_date" in c_data

    # 2. Verificar Ficha inicial (AC #7)
    f1 = client_app.get(f"/api/admin/clients/{biz_id}", headers=admin_headers).json()
    s1 = f1["subscription"]
    assert s1["monthly_price"] == "50000.00"
    assert s1["price_origin"] == "TARIFA"
    assert s1["current_rate_monthly"] == "50000.00"
    assert s1["price_changes"] == []

    # 3. PATCH especial: cambiar monthly_price a 40000 (AC #8)
    res_p1 = client_app.patch(
        f"/api/admin/clients/{biz_id}/subscription",
        headers=admin_headers,
        json={"monthly_price": "40000.00", "reason": "Acuerdo comercial de bienvenida"},
    )
    assert res_p1.status_code == 200
    f2 = res_p1.json()
    s2 = f2["subscription"]
    assert s2["monthly_price"] == "40000.00"
    assert s2["price_origin"] == "ESPECIAL"
    assert s2["price_note"] == "Acuerdo comercial de bienvenida"
    assert s2["final_price"] == "114000.00"  # 40000 * 3 = 120000 - 5% = 114000
    assert len(s2["price_changes"]) == 1
    ch1 = s2["price_changes"][0]
    assert ch1["reason"] == "Acuerdo comercial de bienvenida"
    assert ch1["old_monthly_price"] == "50000.00"
    assert ch1["new_monthly_price"] == "40000.00"

    # 4. PATCH volver a tarifa vigente (use_current_rate=true)
    res_p2 = client_app.patch(
        f"/api/admin/clients/{biz_id}/subscription",
        headers=admin_headers,
        json={"use_current_rate": True, "reason": "Fin de periodo promocional"},
    )
    assert res_p2.status_code == 200
    f3 = res_p2.json()
    s3 = f3["subscription"]
    assert s3["monthly_price"] == "50000.00"
    assert s3["price_origin"] == "TARIFA"
    assert s3["final_price"] == "142500.00"
    assert len(s3["price_changes"]) == 2

    # 5. Validaciones 422:
    # use_current_rate con monthly_price
    assert client_app.patch(
        f"/api/admin/clients/{biz_id}/subscription",
        headers=admin_headers,
        json={"use_current_rate": True, "monthly_price": "45000", "reason": "Test"},
    ).status_code == 422

    # Sin cambios especificados
    assert client_app.patch(
        f"/api/admin/clients/{biz_id}/subscription",
        headers=admin_headers,
        json={"reason": "Test sin cambios"},
    ).status_code == 422

    # Sin motivo
    assert client_app.patch(
        f"/api/admin/clients/{biz_id}/subscription",
        headers=admin_headers,
        json={"plan": "ANUAL", "reason": ""},
    ).status_code == 422


# ==============================================================================
# 10. Pagos y Aviso de Monto (AC #10)
# ==============================================================================


def test_payment_amount_mismatch_and_allow_mismatch(client_app, admin_headers, db_session):
    """409 AMOUNT_MISMATCH con cuerpo completo y 201 con allow_mismatch."""
    # Crear cliente
    res_create = client_app.post(
        "/api/admin/clients",
        headers=admin_headers,
        json={
            "person_type": "PERSONA",
            "contact_name": "Cliente Pagos Test",
            "phone": "3003334455",
            "document_number": "1000666777",
            "plan": "TRIMESTRAL",
        },
    )
    biz_id = res_create.json()["business_id"]

    # 1. Pago con monto diferente sin allow_mismatch -> 409 AMOUNT_MISMATCH
    res_mismatch = client_app.post(
        f"/api/admin/clients/{biz_id}/payments",
        headers=admin_headers,
        json={"amount": "140000", "reference": "TR-MISMATCH"},
    )
    assert res_mismatch.status_code == 409
    err = res_mismatch.json()
    assert err["code"] == "AMOUNT_MISMATCH"
    assert err["expected_amount"] == "142500.00"
    assert err["amount"] == "140000.00"
    assert err["difference"] == "-2500.00"

    # Verificar que NO se registró pago en db
    biz = db_session.query(Business).filter(Business.id == biz_id).first()
    sub = db_session.query(Subscription).filter(Subscription.client_id == biz.client_id).first()
    pay_count = db_session.query(PaymentRecord).filter(PaymentRecord.subscription_id == sub.id).count()
    assert pay_count == 0

    # 2. Pago con allow_mismatch: true -> 201 exitoso
    res_ok_mismatch = client_app.post(
        f"/api/admin/clients/{biz_id}/payments",
        headers=admin_headers,
        json={"amount": "140000", "reference": "TR-OK-MISMATCH", "allow_mismatch": True},
    )
    assert res_ok_mismatch.status_code == 201
    pay_data = res_ok_mismatch.json()
    assert pay_data["amount"] == "140000.00"
    assert pay_data["expected_amount"] == "142500.00"
    assert pay_data["difference"] == "-2500.00"

    # 3. Pago con monto exacto sin bandera -> 201 exitoso
    res_exact = client_app.post(
        f"/api/admin/clients/{biz_id}/payments",
        headers=admin_headers,
        json={"amount": "142500", "reference": "TR-EXACT"},
    )
    assert res_exact.status_code == 201

    # 4. Ficha de pagos suma expected_amount
    ficha = client_app.get(f"/api/admin/clients/{biz_id}", headers=admin_headers).json()
    payments = ficha["recent_payments"]
    assert len(payments) == 2
    for p in payments:
        assert p["expected_amount"] == "142500.00"
