"""Pruebas del endpoint GET /api/calendar/{business_id} (Story 4.1b - AC #4)."""

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api.app import app
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import (
    Business,
    DIANTaxCalendar,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    IVA_PERIODICITY_BIMESTRAL,
    Subscription,
    User,
)


@pytest.fixture
def db_session():
    """Base de datos en memoria compartida para la prueba."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_calendar_2026(db):
    """Siembra obligaciones mínimas del calendario 2026."""
    rows = [
        DIANTaxCalendar(
            tax_type="IVA_BIMESTRAL",
            fiscal_year=2026,
            period_label="Ene – Feb 2026",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 2, 28),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=0,
            deadline_date=date(2026, 3, 20),
            description="Declaración y pago",
        ),
        DIANTaxCalendar(
            tax_type="IVA_BIMESTRAL",
            fiscal_year=2026,
            period_label="Mar – Abr 2026",
            period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 30),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=0,
            deadline_date=date(2026, 5, 20),
            description="Declaración y pago",
        ),
        DIANTaxCalendar(
            tax_type="RETEFUENTE",
            fiscal_year=2026,
            period_label="Ene 2026",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=0,
            deadline_date=date(2026, 2, 20),
            description="Declaración y pago",
        ),
        DIANTaxCalendar(
            tax_type="RENTA_PERSONAS_NATURALES",
            fiscal_year=2026,
            period_label="Renta año gravable 2025",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            key_length=2,
            key_from=0,
            key_to=99,
            installment=0,
            deadline_date=date(2026, 8, 12),
            description="Declaración y pago",
        ),
    ]
    db.add_all(rows)
    db.commit()


def _seed_owner(
    db,
    suffix: str,
    nit: str,
    status: str = "ACTIVO",
    income_source: str = INCOME_SOURCE_DIAN,
    iva_periodicity: Optional[str] = IVA_PERIODICITY_BIMESTRAL,
    is_withholding_agent: bool = True,
    taxpayer_type: str = "PERSONA_NATURAL",
) -> None:
    """Crea usuario, negocio y suscripción para pruebas de rutas."""
    user = User(id=f"usr-{suffix}", email=f"{suffix}@test.co", full_name=suffix, role="CLIENT", is_active=True)
    business = Business(
        id=f"biz-{suffix}",
        client_id=user.id,
        legal_name=f"Empresa {suffix} SAS",
        commercial_name=f"Comercial {suffix}",
        nit=nit,
        dv="7",
        taxpayer_type=taxpayer_type,
        iva_periodicity=iva_periodicity,
        is_withholding_agent=is_withholding_agent,
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
    """Siembra el calendario 2026 y diversos negocios para las pruebas."""
    _seed_calendar_2026(db_session)
    _seed_owner(db_session, "ana", "901111111", income_source=INCOME_SOURCE_DIAN)
    _seed_owner(db_session, "manual", "902222222", income_source=INCOME_SOURCE_MANUAL_SALES)
    _seed_owner(db_session, "bloq", "903333333", status="BLOQUEADO", income_source=INCOME_SOURCE_DIAN)
    _seed_owner(db_session, "otro", "904444444", income_source=INCOME_SOURCE_DIAN)
    return db_session


def _use_db(db) -> None:
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db


@pytest.fixture
def client(seeded, bearer):
    """Cliente HTTP autenticado como usr-ana."""
    _use_db(seeded)
    yield TestClient(app, headers=bearer("usr-ana"))
    app.dependency_overrides.clear()


def test_get_calendar_200_contract(client, monkeypatch):
    """200 con el contrato exacto: claves y tipos esperados, fecha_limite ISO."""
    # Fijar today_bogota en el módulo de rutas mediante monkeypatch
    monkeypatch.setattr("dian_automation.api.routes_calendar.today_bogota", lambda: date(2026, 5, 10))

    response = client.get("/api/calendar/biz-ana")
    assert response.status_code == 200

    data = response.json()
    assert "obligaciones" in data
    assert isinstance(data["obligaciones"], list)
    assert len(data["obligaciones"]) > 0

    iso_date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    for ob in data["obligaciones"]:
        # Contrato exacto de claves
        assert set(ob.keys()) == {"tax_type", "etiqueta", "fecha_limite", "estado", "dias"}

        # Tipos y valores esperados
        assert isinstance(ob["tax_type"], str)
        assert isinstance(ob["etiqueta"], str)
        assert isinstance(ob["fecha_limite"], str)
        assert iso_date_pattern.match(ob["fecha_limite"]) is not None
        assert ob["estado"] in ("completado", "proximo", "aldia")

        if ob["estado"] == "completado":
            assert ob["dias"] is None
        else:
            assert isinstance(ob["dias"], int)
            assert ob["dias"] >= 0


def test_get_calendar_401_without_jwt(seeded):
    """401 cuando la petición no incluye cabecera Authorization (sin sesión)."""
    _use_db(seeded)
    unauthed_client = TestClient(app)
    response = unauthed_client.get("/api/calendar/biz-ana")
    assert response.status_code == 401


def test_get_calendar_404_foreign_business(client):
    """404 si el negocio solicitado pertenece a otro usuario."""
    response = client.get("/api/calendar/biz-otro")
    assert response.status_code == 404
    assert "No se encontró el negocio" in response.json()["detail"]


def test_get_calendar_404_manual_sales(seeded, bearer):
    """404 con mensaje específico si el negocio es de tipo MANUAL_SALES."""
    _use_db(seeded)
    manual_client = TestClient(app, headers=bearer("usr-manual"))
    response = manual_client.get("/api/calendar/biz-manual")
    assert response.status_code == 404
    assert response.json()["detail"] == "Este servicio no aplica a tu tipo de negocio."


def test_get_calendar_403_subscription_blocked(seeded, bearer):
    """403 Forbidden cuando el usuario tiene la suscripción en estado BLOQUEADO."""
    _use_db(seeded)
    bloq_client = TestClient(app, headers=bearer("usr-bloq"))
    response = bloq_client.get("/api/calendar/biz-bloq")
    assert response.status_code == 403


def test_get_calendar_409_calendar_not_loaded(client, monkeypatch):
    """409 Conflict si el calendario del año consultado no está cargado."""
    # Simulamos que la fecha actual cae en el año 2028 (año no sembrado en la base)
    monkeypatch.setattr("dian_automation.api.routes_calendar.today_bogota", lambda: date(2028, 5, 10))

    response = client.get("/api/calendar/biz-ana")
    assert response.status_code == 409
    assert "2028" in response.json()["detail"]
