"""Pruebas unitarias para Story 2.3: Bot de Telegram para Contribuyentes (Clientes)."""

import pytest
from datetime import datetime, date, timedelta
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import (
    Base,
    User,
    Business,
    MonthlyTaxSummary,
    Invoice,
    DIANTaxCalendar,
    Subscription,
)
from dian_automation.telegram.client_bot import ClientTelegramBot


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria con semillas de prueba."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = TestingSessionLocal()

    # 1. Cliente activo y vinculado (Andrea Torres)
    user_andrea = User(
        id="usr-andrea-torres",
        email="andrea.torres@empresa.co",
        full_name="Andrea Torres",
        role="CLIENT",
        telegram_chat_id=777888999,
        is_telegram_linked=True,
        is_active=True,
    )
    # NIT 901008579 -> último dígito es 9
    biz_andrea = Business(
        id="biz-andrea",
        client_id=user_andrea.id,
        legal_name="Andrea Torres Diseño SAS",
        commercial_name="Torres Branding & Studio",
        nit="901008579",
        dv="7",
        is_active=True,
    )
    sub_andrea = Subscription(
        id="sub-andrea",
        client_id=user_andrea.id,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date.today() - timedelta(days=10),
        cutoff_date=date.today() + timedelta(days=80),
        grace_period_end=date.today() + timedelta(days=83),
        status="ACTIVO",
    )

    # 2. Cliente con suscripción bloqueada (Carlos Mora)
    user_bloqueado = User(
        id="usr-carlos-bloqueado",
        email="carlos@mora.co",
        full_name="Carlos Mora",
        role="CLIENT",
        telegram_chat_id=111000111,
        is_telegram_linked=True,
        is_active=True,
    )
    biz_bloqueado = Business(
        id="biz-carlos",
        client_id=user_bloqueado.id,
        legal_name="Comercial Mora SAS",
        commercial_name="Mora Distribuciones",
        nit="800197268",
        dv="4",
        is_active=True,
    )
    sub_bloqueada = Subscription(
        id="sub-carlos-bloq",
        client_id=user_bloqueado.id,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date.today() - timedelta(days=100),
        cutoff_date=date.today() - timedelta(days=10),
        grace_period_end=date.today() - timedelta(days=7),
        status="BLOQUEADO",
    )

    # 3. Calendario tributario DIAN (dígito 9 y dígito 4)
    cal_iva_9 = DIANTaxCalendar(
        tax_type="IVA BIMESTRAL",
        fiscal_year=2026,
        period_label="Jul – Ago 2026",
        nit_last_digit=9,
        deadline_date=date.today() + timedelta(days=4),
        description="Declaración bimestral formulario 300",
    )
    cal_rete_9 = DIANTaxCalendar(
        tax_type="RETEFUENTE",
        fiscal_year=2026,
        period_label="Agosto 2026",
        nit_last_digit=9,
        deadline_date=date.today() + timedelta(days=12),
        description="Declaración mensual formulario 350",
    )
    cal_iva_4 = DIANTaxCalendar(
        tax_type="IVA BIMESTRAL",
        fiscal_year=2026,
        period_label="Jul – Ago 2026",
        nit_last_digit=4,
        deadline_date=date.today() + timedelta(days=2),
        description="Declaración bimestral formulario 300",
    )

    db.add_all([
        user_andrea, biz_andrea, sub_andrea,
        user_bloqueado, biz_bloqueado, sub_bloqueada,
        cal_iva_9, cal_rete_9, cal_iva_4,
    ])
    db.commit()
    db.close()

    return TestingSessionLocal


def test_client_unlinked_access_denied(db_session_factory):
    """Un chat_id no vinculado es rechazado con mensaje informativo."""
    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_resumen(sender_chat_id=999999999, db=db)
        assert resp["success"] is False
        assert resp["reason"] == "UNLINKED"
        assert "Cuenta no vinculada" in resp["message"]

        # Mensaje de ayuda para no vinculados
        help_msg = ClientTelegramBot.handle_help(sender_chat_id=999999999, db=db)
        assert "Cuenta no vinculada" in help_msg
    finally:
        db.close()


def test_client_resumen_success_with_iva_balance_positive(db_session_factory):
    """El comando /resumen muestra datos correctos con saldo a pagar."""
    db = db_session_factory()
    try:
        # Registrar resumen mensual para Andrea
        summary = MonthlyTaxSummary(
            business_id="biz-andrea",
            period_year_month="2026-08",
            total_invoiced_net=Decimal("12500000.00"),
            iva_generado=Decimal("2375000.00"),
            iva_descontable=Decimal("1425000.00"),
            iva_balance=Decimal("950000.00"),  # A pagar
            rete_iva_total=Decimal("150000.00"),
            rete_renta_total=Decimal("312500.00"),
            rete_ica_total=Decimal("120000.00"),
            total_invoices_count=18,
            variation_vs_previous_pct=Decimal("12.50"),
        )
        db.add(summary)
        db.commit()

        resp = ClientTelegramBot.handle_resumen(sender_chat_id=777888999, db=db)
        assert resp["success"] is True
        assert resp["has_data"] is True
        assert resp["period"] == "2026-08"
        assert resp["total_invoiced"] == 12500000.0
        assert resp["iva_balance"] == 950000.0

        msg = resp["message"]
        assert "RESUMEN FISCAL DEL MES (2026-08)" in msg
        assert "Torres Branding & Studio" in msg
        assert "NIT 901008579-7" in msg
        assert "$12,500,000 COP" in msg
        assert "+12.5%" in msg
        assert "Saldo a Pagar DIAN" in msg
        assert "$950,000 COP" in msg
        assert "ReteIVA" in msg
    finally:
        db.close()


def test_client_resumen_saldo_a_favor(db_session_factory):
    """El comando /resumen detecta saldo a favor de IVA cuando iva_balance < 0."""
    db = db_session_factory()
    try:
        summary = MonthlyTaxSummary(
            business_id="biz-andrea",
            period_year_month="2026-07",
            total_invoiced_net=Decimal("4000000.00"),
            iva_generado=Decimal("760000.00"),
            iva_descontable=Decimal("1140000.00"),
            iva_balance=Decimal("-380000.00"),  # A favor
            rete_iva_total=Decimal("0.00"),
            rete_renta_total=Decimal("0.00"),
            rete_ica_total=Decimal("0.00"),
            total_invoices_count=4,
            variation_vs_previous_pct=Decimal("-5.00"),
        )
        db.add(summary)
        db.commit()

        resp = ClientTelegramBot.handle_resumen(sender_chat_id=777888999, db=db, target_period="2026-07")
        assert resp["success"] is True
        assert "*Saldo a Favor:* $380,000 COP" in resp["message"]
        assert "-5.0%" in resp["message"]
    finally:
        db.close()


def test_client_resumen_sin_datos(db_session_factory):
    """El comando /resumen informa amablemente si el negocio no tiene resúmenes aún."""
    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_resumen(sender_chat_id=777888999, db=db)
        assert resp["success"] is True
        assert resp["has_data"] is False
        assert "Aún no dispones de resúmenes fiscales liquidados" in resp["message"]
    finally:
        db.close()


def test_client_facturas_last_four(db_session_factory):
    """El comando /facturas devuelve únicamente las últimas 4 facturas emitidas."""
    db = db_session_factory()
    try:
        base_time = datetime(2026, 8, 10, 10, 0, 0)
        # 5 facturas emitidas y 1 recibida
        for i in range(1, 6):
            inv = Invoice(
                id=f"inv-emitida-{i}",
                business_id="biz-andrea",
                document_type="Factura electrónica de venta",
                cufe=f"cufe-emitida-{i}",
                folio=f"100{i}",
                prefix="FE",
                issue_date=base_time + timedelta(days=i),
                issuer_nit="901008579",
                issuer_name="Andrea Torres Diseño SAS",
                receiver_nit=f"90011122{i}",
                receiver_name=f"Cliente Empresa {i} SAS",
                total=Decimal(f"{i * 1000000}.00"),
                group_type="Emitido",
                dian_status="Aceptada",
            )
            db.add(inv)

        # Factura recibida (debe ser ignorada por /facturas de emisión)
        inv_recibida = Invoice(
            id="inv-recibida-1",
            business_id="biz-andrea",
            document_type="Factura electrónica de venta",
            cufe="cufe-recibida-1",
            folio="9999",
            prefix="REC",
            issue_date=base_time + timedelta(days=10),
            issuer_nit="800111222",
            issuer_name="Proveedor Mayorista SAS",
            receiver_nit="901008579",
            receiver_name="Andrea Torres Diseño SAS",
            total=Decimal("5000000.00"),
            group_type="Recibido",
            dian_status="Aceptada",
        )
        db.add(inv_recibida)
        db.commit()

        resp = ClientTelegramBot.handle_facturas(sender_chat_id=777888999, db=db, limit=4)
        assert resp["success"] is True
        assert resp["count"] == 4

        msg = resp["message"]
        # Debe contener la 5, 4, 3, 2 (más recientes)
        assert "FE-1005" in msg
        assert "FE-1004" in msg
        assert "FE-1003" in msg
        assert "FE-1002" in msg
        # La 1001 quedó excluida por el límite de 4
        assert "FE-1001" not in msg
        # La recibida no debe aparecer
        assert "REC-9999" not in msg
    finally:
        db.close()


def test_client_facturas_vacias(db_session_factory):
    """El comando /facturas informa si no hay facturas emitidas."""
    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_facturas(sender_chat_id=777888999, db=db)
        assert resp["success"] is True
        assert resp["count"] == 0
        assert "No se registran facturas electrónicas emitidas" in resp["message"]
    finally:
        db.close()


def test_client_vencimientos_filtered_by_nit_last_digit(db_session_factory):
    """El comando /vencimientos filtra por el último dígito del NIT del negocio (9)."""
    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_vencimientos(
            sender_chat_id=777888999, db=db, reference_date=date.today()
        )
        assert resp["success"] is True
        assert resp["last_digit"] == 9
        assert resp["count"] == 2  # cal_iva_9 y cal_rete_9

        msg = resp["message"]
        assert "Último dígito: *9*" in msg
        assert "IVA BIMESTRAL" in msg
        assert "RETEFUENTE" in msg
        assert "Agosto 2026" in msg
        # No debe contener el dígito 4
        assert "Dígito: `4`" not in msg
    finally:
        db.close()


def test_client_subscription_blocked_rejection(db_session_factory):
    """Un cliente con suscripción BLOQUEADA es rechazado con mensaje de servicio suspendido."""
    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_resumen(sender_chat_id=111000111, db=db)
        assert resp["success"] is False
        assert resp["reason"] == "SUBSCRIPTION_BLOCKED"
        assert "Servicio Suspendido" in resp["message"]

        # Probar a través del enrutador
        router_resp = ClientTelegramBot.handle_client_message(
            sender_chat_id=111000111, text="/facturas", db=db
        )
        assert "Servicio Suspendido" in router_resp
    finally:
        db.close()


def test_client_dashboard_link(db_session_factory):
    """El comando /dashboard entrega el enlace al frontend web con el NIT real del cliente."""
    from dian_automation.config import config

    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_dashboard(sender_chat_id=777888999, db=db)
        assert resp["success"] is True
        assert resp["link"] == f"{config.kontable_web_url}?nit=901008579"
        assert resp["link"] in resp["message"]

        router_resp = ClientTelegramBot.handle_client_message(
            sender_chat_id=777888999, text="/dashboard", db=db
        )
        assert "901008579" in router_resp
    finally:
        db.close()


def test_client_router_and_help(db_session_factory):
    """Enrutador de comandos despacha /ayuda, /help, /start y mensajes no reconocidos."""
    db = db_session_factory()
    try:
        help_resp = ClientTelegramBot.handle_client_message(
            sender_chat_id=777888999, text="/ayuda", db=db
        )
        assert "¡Hola, bienvenido a Kontable Bot!" in help_resp
        assert "/resumen" in help_resp
        assert "/facturas" in help_resp
        assert "/vencimientos" in help_resp
        assert "/dashboard" in help_resp

        start_resp = ClientTelegramBot.handle_client_message(
            sender_chat_id=777888999, text="/start", db=db
        )
        assert "/resumen" in start_resp

        unknown_resp = ClientTelegramBot.handle_client_message(
            sender_chat_id=777888999, text="/comando_invalido", db=db
        )
        assert "No reconozco ese comando" in unknown_resp
    finally:
        db.close()
