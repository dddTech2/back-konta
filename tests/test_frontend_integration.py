"""Pruebas automatizadas de integración para el frontend móvil Konta."""

import os
import re
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


FRONTEND_HTML_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ProyectoDianFront", "kontable-prototipo_1.html")
)


def test_frontend_file_exists_and_uninegocio_rules():
    """Valida que el archivo HTML exista y cumpla las reglas de uninegocio sin conmutadores."""
    assert os.path.isfile(FRONTEND_HTML_PATH), f"No se encontró el archivo {FRONTEND_HTML_PATH}"

    with open(FRONTEND_HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    # 1. No debe existir llamada a alternar o conmutar negocios en el hero
    assert "goto('negocios')" not in html, "El selector multi-negocio no debe existir en el hero"
    assert "screenNegocios" not in html, "La pantalla screenNegocios debe haber sido retirada de la visual"

    # 2. Debe existir la pantalla de suspensión para 403 Forbidden
    assert "screenSuspended" in html, "Debe existir screenSuspended para manejar 403 Forbidden"
    assert "SERVICIO SUSPENDIDO (403)" in html
    assert "https://wa.me/573001234567" in html, "Debe incluir botón de contacto directo a Katerinn por WhatsApp"

    # 3. Debe existir la capa de sincronización con la API
    assert "syncWithBackend" in html, "Debe existir la función de sincronización con la API"
    assert "/api/dashboard/" in html
    assert "/api/iva/" in html
    assert "/api/invoices/" in html

    # 4. Debe contar con el botón o toggle de simulación de 403
    assert "simulateLockoutToggle" in html


@pytest.fixture(name="client_and_db")
def fixture_client_and_db():
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    db = TestingSessionLocal()

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    yield client, db

    app.dependency_overrides.clear()
    db.close()


def test_api_contract_matches_frontend_expectations(client_and_db, bearer):
    """Valida que los payloads devueltos por la API cumplan exactamente con las propiedades que lee el frontend."""
    client, db = client_and_db
    client.headers.update(bearer("usr-front-test"))

    # Sembrar datos de Andrea Torres
    user = User(
        id="usr-front-test",
        email="andrea.front@test.com",
        full_name="Andrea Torres",
        role="CLIENT",
        is_active=True,
    )
    db.add(user)

    biz = Business(
        id="biz-front-test",
        client_id=user.id,
        legal_name="Andrea Torres Diseño SAS",
        commercial_name="Andrea Torres Diseño",
        nit="901234567",
        dv="1",
        is_active=True,
    )
    db.add(biz)

    sub = Subscription(
        id="sub-front-test",
        client_id=user.id,
        plan="TRIMESTRAL",
        start_date=date.today() - timedelta(days=10),
        cutoff_date=date.today() + timedelta(days=80),
        grace_period_end=date.today() + timedelta(days=83),
        status="ACTIVO",
        base_price=Decimal("150000.00"),
        discount_rate=Decimal("5.00"),
        final_price=Decimal("142500.00"),
    )
    db.add(sub)

    summary = MonthlyTaxSummary(
        business_id=biz.id,
        period_year_month="2026-08",
        total_invoiced_net=Decimal("12450000.00"),
        iva_generado=Decimal("2365500.00"),
        iva_descontable=Decimal("540000.00"),
        iva_balance=Decimal("1825500.00"),
        total_invoices_count=18,
        variation_vs_previous_pct=Decimal("8.00"),
    )
    db.add(summary)
    db.commit()

    # 1. Contrato Dashboard
    r_dash = client.get(f"/api/dashboard/{biz.nit}")
    assert r_dash.status_code == 200
    d_dash = r_dash.json()

    assert "business" in d_dash
    assert d_dash["business"]["commercial_name"] == "Andrea Torres Diseño"
    assert "resumen" in d_dash
    assert "total" in d_dash["resumen"]
    assert "ivaAcumulado" in d_dash["resumen"]
    assert "historico" in d_dash
    assert "facturasRecientes" in d_dash
    assert "suscripcion" in d_dash

    # 2. Contrato IVA
    r_iva = client.get(f"/api/iva/{biz.nit}")
    assert r_iva.status_code == 200
    d_iva = r_iva.json()
    assert "periodos" in d_iva
    p0 = d_iva["periodos"][0]
    assert "generado" in p0
    assert "descontable" in p0
    assert "saldo" in p0
    assert "pct" in p0

    # 3. Contrato Bloqueo 403
    sub.status = "BLOQUEADO"
    db.commit()

    r_lock = client.get(f"/api/dashboard/{biz.nit}")
    assert r_lock.status_code == 403
    d_lock = r_lock.json()
    assert d_lock["error"] == "SUBSCRIPTION_BLOCKED"
    assert "message" in d_lock
    assert d_lock["redirect_url"] == "/servicio-suspendido"
