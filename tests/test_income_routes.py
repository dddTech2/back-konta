"""Pruebas del endpoint GET /api/income-summary/{business_id} (Story 6.3)."""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api.app import app
from dian_automation.core import income_service
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import (
    Business,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    Invoice,
    Sale,
    Subscription,
    User,
)

_BOGOTA_TZ = ZoneInfo("America/Bogota")


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_owner(db, suffix: str, nit: str, status: str = "ACTIVO", income_source: str = INCOME_SOURCE_MANUAL_SALES) -> None:
    user = User(id=f"usr-{suffix}", email=f"{suffix}@test.co", full_name=suffix, role="CLIENT", is_active=True)
    business = Business(
        id=f"biz-{suffix}",
        client_id=user.id,
        legal_name=f"Empresa {suffix} SAS",
        commercial_name=f"Comercial {suffix}",
        nit=nit,
        dv="7",
        income_source=income_source,
        is_active=True,
    )
    sub = Subscription(
        id=f"sub-{suffix}",
        client_id=user.id,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date.today() - timedelta(days=10),
        cutoff_date=date.today() + timedelta(days=80),
        grace_period_end=date.today() + timedelta(days=83),
        status=status,
    )
    db.add_all([user, business, sub])
    db.commit()


@pytest.fixture
def seeded(db_session):
    _seed_owner(db_session, "ana", "901111111", income_source=INCOME_SOURCE_MANUAL_SALES)
    _seed_owner(db_session, "dian", "902222222", income_source=INCOME_SOURCE_DIAN)
    _seed_owner(db_session, "bloq", "903333333", status="BLOQUEADO", income_source=INCOME_SOURCE_MANUAL_SALES)
    _seed_owner(db_session, "otro", "904444444", income_source=INCOME_SOURCE_MANUAL_SALES)
    return db_session


def _use_db(db) -> None:
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db


@pytest.fixture
def client(seeded, bearer):
    _use_db(seeded)
    yield TestClient(app, headers=bearer("usr-ana"))
    app.dependency_overrides.clear()


def test_income_summary_success_with_data(client, seeded):
    """200 con datos: ventas del mes, factura Recibido con IVA, nota crédito que resta, Emitido que no cuenta."""
    # Ventas en mayo 2026: 1500000 + 500000 = 2000000
    s1 = Sale(
        business_id="biz-ana",
        total_amount=Decimal("1500000.00"),
        recorded_via="WEB",
        recorded_by_user_id="usr-ana",
        sale_date=date(2026, 5, 10),
        created_at=datetime(2026, 5, 10, 10, 0),
    )
    s2 = Sale(
        business_id="biz-ana",
        total_amount=Decimal("500000.00"),
        recorded_via="WEB",
        recorded_by_user_id="usr-ana",
        sale_date=date(2026, 5, 20),
        created_at=datetime(2026, 5, 20, 15, 0),
    )
    seeded.add_all([s1, s2])

    # Facturas en mayo 2026:
    # Recibido normal: 800000
    inv_rec = Invoice(
        id=str(uuid.uuid4()),
        business_id="biz-ana",
        cufe=str(uuid.uuid4()),
        document_type="Factura electrónica de venta",
        group_type="Recibido",
        issue_date=datetime(2026, 5, 12, 10, 0),
        issuer_nit="800111222",
        issuer_name="Proveedor A",
        receiver_nit="901111111",
        receiver_name="Empresa ana SAS",
        total=Decimal("800000.00"),
        iva=Decimal("127731.00"),
    )
    # Recibido nota crédito: 100000 (debe restar a egresos)
    inv_nc = Invoice(
        id=str(uuid.uuid4()),
        business_id="biz-ana",
        cufe=str(uuid.uuid4()),
        document_type="Nota de Crédito",
        group_type="Recibido",
        issue_date=datetime(2026, 5, 15, 11, 0),
        issuer_nit="800111222",
        issuer_name="Proveedor A",
        receiver_nit="901111111",
        receiver_name="Empresa ana SAS",
        total=Decimal("100000.00"),
        iva=Decimal("15966.00"),
    )
    # Emitido: 3000000 (NO debe contar como egreso ni ingreso)
    inv_em = Invoice(
        id=str(uuid.uuid4()),
        business_id="biz-ana",
        cufe=str(uuid.uuid4()),
        document_type="Factura electrónica de venta",
        group_type="Emitido",
        issue_date=datetime(2026, 5, 18, 9, 0),
        issuer_nit="901111111",
        issuer_name="Empresa ana SAS",
        receiver_nit="900555666",
        receiver_name="Cliente X",
        total=Decimal("3000000.00"),
        iva=Decimal("478991.00"),
    )
    seeded.add_all([inv_rec, inv_nc, inv_em])
    seeded.commit()

    response = client.get("/api/income-summary/biz-ana?month=2026-05")
    assert response.status_code == 200

    data = response.json()
    assert data["month"] == "2026-05"
    assert data["ingresos"] == "2000000.00"
    assert data["egresos"] == "700000.00"
    assert data["utilidad"] == "1300000.00"

    # Historial de 6 meses
    historial = data["historial"]
    assert len(historial) == 6
    months_expected = ["2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]
    assert [item["month"] for item in historial] == months_expected

    last_item = historial[-1]
    assert last_item["month"] == "2026-05"
    assert last_item["ingresos"] == "2000000.00"
    assert last_item["egresos"] == "700000.00"
    assert last_item["utilidad"] == "1300000.00"

    # Verificar que los valores en historial sean cadenas decimales con 2 decimales
    for item in historial:
        assert isinstance(item["ingresos"], str)
        assert "." in item["ingresos"] and len(item["ingresos"].split(".")[1]) == 2
        assert isinstance(item["egresos"], str)
        assert "." in item["egresos"] and len(item["egresos"].split(".")[1]) == 2
        assert isinstance(item["utilidad"], str)
        assert "." in item["utilidad"] and len(item["utilidad"].split(".")[1]) == 2


def test_income_summary_default_month_is_current_bogota(client):
    """Sin parámetro month usa el mes en curso en hora Bogotá."""
    now_bogota = datetime.now(_BOGOTA_TZ)
    current_month = now_bogota.strftime("%Y-%m")

    response = client.get("/api/income-summary/biz-ana")
    assert response.status_code == 200
    data = response.json()
    assert data["month"] == current_month
    assert len(data["historial"]) == 6
    assert data["historial"][-1]["month"] == current_month


def test_income_summary_empty_month_returns_zeros(client):
    """Un mes sin movimientos devuelve '0.00' en ingresos, egresos y utilidad."""
    response = client.get("/api/income-summary/biz-ana?month=2024-01")
    assert response.status_code == 200
    data = response.json()
    assert data["month"] == "2024-01"
    assert data["ingresos"] == "0.00"
    assert data["egresos"] == "0.00"
    assert data["utilidad"] == "0.00"


@pytest.mark.parametrize("invalid_month", ["2026-13", "2026-00", "abc", "2026", "2026-5", "2026/05"])
def test_income_summary_invalid_month_returns_422(client, invalid_month):
    """Formato de mes inválido devuelve 422 con el detalle de IncomeError."""
    response = client.get(f"/api/income-summary/biz-ana?month={invalid_month}")
    assert response.status_code == 422
    assert "inválido" in response.json()["detail"].lower()


def test_income_summary_dian_business_returns_404(seeded, bearer):
    """Negocio con income_source DIAN devuelve 404."""
    _use_db(seeded)
    dian_client = TestClient(app, headers=bearer("usr-dian"))
    response = dian_client.get("/api/income-summary/biz-dian")
    assert response.status_code == 404
    assert response.json()["detail"] == "Este servicio no aplica a tu tipo de negocio."
    app.dependency_overrides.clear()


def test_income_summary_unauthorized_or_other_business_returns_404(client):
    """Negocio ajeno o inexistente devuelve 404."""
    response_alien = client.get("/api/income-summary/biz-otro")
    assert response_alien.status_code == 404

    response_nonexistent = client.get("/api/income-summary/biz-inexistente")
    assert response_nonexistent.status_code == 404


def test_income_summary_blocked_subscription_returns_403(seeded, bearer):
    """Suscripción bloqueada devuelve 403."""
    _use_db(seeded)
    bloq_client = TestClient(app, headers=bearer("usr-bloq"))
    response = bloq_client.get("/api/income-summary/biz-bloq")
    assert response.status_code == 403
    app.dependency_overrides.clear()


def test_income_summary_without_jwt_returns_401(seeded):
    """Llamada sin token de autenticación devuelve 401."""
    _use_db(seeded)
    anon_client = TestClient(app)
    response = anon_client.get("/api/income-summary/biz-ana")
    assert response.status_code == 401
    app.dependency_overrides.clear()


def test_income_summary_lookup_by_nit_works(client):
    """El NIT funciona como identificador en la URL en lugar del ID interno."""
    response = client.get("/api/income-summary/901111111?month=2026-05")
    assert response.status_code == 200
    assert response.json()["month"] == "2026-05"


def test_income_summary_delegates_to_income_service_without_own_calculation(client, monkeypatch):
    """La ruta no realiza sumas por su cuenta; delega completamente a income_service.summary e income_service.history."""
    calls_summary = []
    calls_history = []

    mock_summary_result = {
        "month": "2026-08",
        "ingresos": Decimal("987654.32"),
        "egresos": Decimal("123456.78"),
        "utilidad": Decimal("864197.54"),
    }
    mock_history_result = [
        {"month": f"2026-0{i}", "ingresos": Decimal(f"{i}00.00"), "egresos": Decimal(f"{i}0.00"), "utilidad": Decimal(f"{i * 90}.00")}
        for i in range(1, 7)
    ]

    def fake_summary(db, business, month):
        calls_summary.append((business.id, month))
        return mock_summary_result

    def fake_history(db, business, month, months=6):
        calls_history.append((business.id, month, months))
        return mock_history_result

    monkeypatch.setattr(income_service, "summary", fake_summary)
    monkeypatch.setattr(income_service, "history", fake_history)

    response = client.get("/api/income-summary/biz-ana?month=2026-08")
    assert response.status_code == 200

    assert len(calls_summary) == 1
    assert calls_summary[0] == ("biz-ana", "2026-08")
    assert len(calls_history) == 1
    assert calls_history[0][0] == "biz-ana"
    assert calls_history[0][1] == "2026-08"

    body = response.json()
    assert body["month"] == "2026-08"
    assert body["ingresos"] == "987654.32"
    assert body["egresos"] == "123456.78"
    assert body["utilidad"] == "864197.54"
    assert len(body["historial"]) == 6
    assert body["historial"][0]["ingresos"] == "100.00"
    assert body["historial"][-1]["utilidad"] == "540.00"
