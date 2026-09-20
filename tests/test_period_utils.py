"""Pruebas unitarias de period_utils y prueba de integración de ruta con filtro period."""

from datetime import date, datetime, timedelta
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, DateTime, Integer, MetaData, Table, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api.app import app
from dian_automation.core.period_utils import period_clause, period_range
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import Business, Invoice, Subscription, User


def test_period_range_mes_normal():
    rng = period_range("2026-09")
    assert rng == (datetime(2026, 9, 1, 0, 0), datetime(2026, 10, 1, 0, 0))


def test_period_range_diciembre():
    rng = period_range("2026-12")
    assert rng == (datetime(2026, 12, 1, 0, 0), datetime(2027, 1, 1, 0, 0))


def test_period_range_anio():
    rng = period_range("2026")
    assert rng == (datetime(2026, 1, 1, 0, 0), datetime(2027, 1, 1, 0, 0))


def test_period_range_dia():
    rng = period_range("2026-09-15")
    assert rng == (datetime(2026, 9, 15, 0, 0), datetime(2026, 9, 16, 0, 0))


def test_period_range_espacios():
    rng = period_range("   2026-09   ")
    assert rng == (datetime(2026, 9, 1, 0, 0), datetime(2026, 10, 1, 0, 0))


@pytest.mark.parametrize(
    "invalid_input",
    [
        "",
        "   ",
        "abc",
        "2026-13",
        "2026-09-31",
        "2026-9",
    ],
)
def test_period_range_invalid_inputs(invalid_input):
    assert period_range(invalid_input) is None


def test_period_clause_sqlite_memory_boundaries():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    metadata = MetaData()
    test_table = Table(
        "test_events",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("timestamp", DateTime, nullable=False),
    )
    metadata.create_all(bind=engine)

    Session = sessionmaker(bind=engine)
    with Session() as session:
        session.execute(
            test_table.insert(),
            [
                {"id": 1, "timestamp": datetime(2026, 8, 31, 23, 59, 59, 999999)},
                {"id": 2, "timestamp": datetime(2026, 9, 1, 0, 0, 0)},
                {"id": 3, "timestamp": datetime(2026, 9, 15, 12, 0, 0)},
                {"id": 4, "timestamp": datetime(2026, 9, 30, 23, 59, 59, 999999)},
                {"id": 5, "timestamp": datetime(2026, 10, 1, 0, 0, 0)},
            ],
        )
        session.commit()

        # Selecciona exactamente las filas de septiembre 2026
        clause = period_clause(test_table.c.timestamp, "2026-09")
        stmt = select(test_table.c.id).where(clause).order_by(test_table.c.id)
        selected_ids = session.execute(stmt).scalars().all()
        assert selected_ids == [2, 3, 4]

        # Prefijo inválido retorna false() y no selecciona nada
        clause_invalid = period_clause(test_table.c.timestamp, "abc")
        stmt_invalid = select(test_table.c.id).where(clause_invalid)
        empty_ids = session.execute(stmt_invalid).scalars().all()
        assert empty_ids == []


def test_get_invoices_period_filtering(bearer):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    db = TestingSession()

    user = User(
        id="usr-period-test",
        email="period@test.com",
        full_name="Usuario Periodo",
        role="CLIENT",
        is_active=True,
    )
    biz = Business(
        id="biz-period-test",
        client_id=user.id,
        legal_name="Periodo SAS",
        commercial_name="Periodo",
        nit="900888999",
        dv="3",
        is_active=True,
    )
    sub = Subscription(
        id="sub-period-test",
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
    db.add_all([user, biz, sub])

    inv_aug = Invoice(
        id="inv-aug",
        business_id=biz.id,
        cufe="cufe-aug",
        document_type="Factura electrónica de venta",
        issue_date=datetime(2026, 8, 31, 23, 59),
        issuer_nit="900888999",
        issuer_name="Periodo",
        receiver_nit="800111222",
        receiver_name="Cliente Ago",
        total=Decimal("100000.00"),
        group_type="Emitido",
    )
    inv_sep1 = Invoice(
        id="inv-sep-1",
        business_id=biz.id,
        cufe="cufe-sep-1",
        document_type="Factura electrónica de venta",
        issue_date=datetime(2026, 9, 1, 0, 0),
        issuer_nit="900888999",
        issuer_name="Periodo",
        receiver_nit="800111222",
        receiver_name="Cliente Sep 1",
        total=Decimal("200000.00"),
        group_type="Emitido",
    )
    inv_sep2 = Invoice(
        id="inv-sep-2",
        business_id=biz.id,
        cufe="cufe-sep-2",
        document_type="Factura electrónica de venta",
        issue_date=datetime(2026, 9, 25, 14, 0),
        issuer_nit="900888999",
        issuer_name="Periodo",
        receiver_nit="800111222",
        receiver_name="Cliente Sep 2",
        total=Decimal("300000.00"),
        group_type="Emitido",
    )
    inv_oct = Invoice(
        id="inv-oct",
        business_id=biz.id,
        cufe="cufe-oct",
        document_type="Factura electrónica de venta",
        issue_date=datetime(2026, 10, 1, 0, 0),
        issuer_nit="900888999",
        issuer_name="Periodo",
        receiver_nit="800111222",
        receiver_name="Cliente Oct",
        total=Decimal("400000.00"),
        group_type="Emitido",
    )
    db.add_all([inv_aug, inv_sep1, inv_sep2, inv_oct])
    db.commit()

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, headers=bearer("usr-period-test"))
    try:
        r_sep = client.get(f"/api/invoices/{biz.id}?period=2026-09")
        assert r_sep.status_code == 200
        data_sep = r_sep.json()
        assert data_sep["total_count"] == 2
        returned_ids = {inv["id"] for inv in data_sep["invoices"]}
        assert returned_ids == {"inv-sep-1", "inv-sep-2"}

        r_abc = client.get(f"/api/invoices/{biz.id}?period=abc")
        assert r_abc.status_code == 200
        data_abc = r_abc.json()
        assert data_abc["total_count"] == 0
        assert data_abc["invoices"] == []
    finally:
        app.dependency_overrides.clear()
        db.close()
        test_engine.dispose()
