"""Pruebas de la Story 10.4: resumen del perfil mixto por la API y por el bot (`/resumen`)."""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api.app import app
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import Business, Invoice, Sale, Subscription, User
from dian_automation.telegram.client_bot import ClientTelegramBot

CHAT_ID = 444555666
MONTH = "2026-09"


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    user = User(id="usr-mix", email="mix@x.co", full_name="Mixto", role="CLIENT", telegram_chat_id=CHAT_ID,
                is_telegram_linked=True, is_active=True)
    biz = Business(id="biz-mix", client_id=user.id, legal_name="Mixto SAS", commercial_name="Tienda Mixta",
                   nit="900777888", dv="5", income_source="MIXED", vat_responsible=False, is_active=True)
    sub = Subscription(id="sub-mix", client_id=user.id, plan="TRIMESTRAL", discount_rate=Decimal("5.00"),
                       base_price=Decimal("749700.00"), final_price=Decimal("712215.00"),
                       start_date=date.today() - timedelta(days=10), cutoff_date=date.today() + timedelta(days=80),
                       grace_period_end=date.today() + timedelta(days=83), status="ACTIVO")
    session.add_all([user, biz, sub])
    for total, receiver in ((Decimal("500000.00"), "800111"), (Decimal("300000.00"), "800222")):
        session.add(Invoice(id=str(uuid.uuid4()), business_id=biz.id, cufe=str(uuid.uuid4()),
                            document_type="Factura electrónica de venta", group_type="Emitido",
                            issue_date=datetime(2026, 9, 10, 15), issuer_nit=biz.nit, issuer_name=biz.legal_name,
                            receiver_nit=receiver, receiver_name="Cliente", total=total, iva=Decimal("0.00")))
    session.add(Invoice(id=str(uuid.uuid4()), business_id=biz.id, cufe=str(uuid.uuid4()),
                        document_type="Factura electrónica de venta", group_type="Recibido",
                        issue_date=datetime(2026, 9, 12, 15), issuer_nit="800999", issuer_name="Proveedor",
                        receiver_nit=biz.nit, receiver_name=biz.legal_name, total=Decimal("250000.00"),
                        iva=Decimal("0.00")))
    session.add(Sale(business_id=biz.id, total_amount=Decimal("200000.00"), recorded_via="TELEGRAM",
                     recorded_by_user_id=user.id, sale_date=date(2026, 9, 15)))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_income_summary_route_for_mixed_profile(db, bearer):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        res = TestClient(app, headers=bearer("usr-mix")).get(f"/api/income-summary/biz-mix?month={MONTH}")
    finally:
        app.dependency_overrides.clear()

    assert res.status_code == 200
    body = res.json()
    assert (body["emitido"], body["ventas"], body["ingresos"]) == ("800000.00", "200000.00", "1000000.00")
    assert (body["egresos"], body["utilidad"], body["diferencia"]) == ("250000.00", "750000.00", "600000.00")
    assert (body["emitido_pct"], body["ventas_pct"]) == ("80.00", "20.00")
    assert (body["emitido_count"], body["ventas_count"], body["clientes_count"]) == (2, 1, 2)
    september = next(h for h in body["historial"] if h["month"] == MONTH)
    assert (september["emitido"], september["ventas"]) == ("800000.00", "200000.00")


def test_bot_resumen_for_mixed_profile(db):
    resp = ClientTelegramBot.handle_resumen(sender_chat_id=CHAT_ID, db=db, target_period=MONTH)

    assert resp["success"] is True and resp["has_data"] is True
    msg = resp["message"]
    assert "*Facturado:* $800,000" in msg and "(2 facturas)" in msg
    assert "*Ventas:* $200,000" in msg and "(1)" in msg
    assert "*Facturado − Ventas:* $600,000" in msg
    assert "*Ingresos:* $1,000,000" in msg
    assert "*Utilidad estimada:* $750,000" in msg
