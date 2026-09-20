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
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
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
        income_source=INCOME_SOURCE_DIAN,
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

    # 4. Cliente activo con origen MANUAL_SALES (Pedro Gómez)
    user_manual = User(
        id="usr-pedro-manual",
        email="pedro.manual@empresa.co",
        full_name="Pedro Gómez",
        role="CLIENT",
        telegram_chat_id=555666777,
        is_telegram_linked=True,
        is_active=True,
    )
    biz_manual = Business(
        id="biz-pedro-manual",
        client_id=user_manual.id,
        legal_name="Panadería Pedro Gómez SAS",
        commercial_name="Panadería Pedro",
        nit="900888777",
        dv="3",
        income_source=INCOME_SOURCE_MANUAL_SALES,
        is_active=True,
    )
    sub_manual = Subscription(
        id="sub-pedro-manual",
        client_id=user_manual.id,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date.today() - timedelta(days=10),
        cutoff_date=date.today() + timedelta(days=80),
        grace_period_end=date.today() + timedelta(days=83),
        status="ACTIVO",
    )

    db.add_all([
        user_andrea, biz_andrea, sub_andrea,
        user_bloqueado, biz_bloqueado, sub_bloqueada,
        user_manual, biz_manual, sub_manual,
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


# ---------------------------------------------------------------------------
# Story 2.4: /registrar_venta
# ---------------------------------------------------------------------------

ANDREA_CHAT = 777888999
CARLOS_CHAT = 111000111  # suscripción BLOQUEADA
MANUAL_CHAT = 555666777  # negocio MANUAL_SALES


def _send(db_session_factory, text, chat_id=MANUAL_CHAT):
    db = db_session_factory()
    try:
        return ClientTelegramBot.handle_client_message(sender_chat_id=chat_id, text=text, db=db)
    finally:
        db.close()


def _sales(db_session_factory):
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        return db.query(Sale).order_by(Sale.created_at).all()
    finally:
        db.close()


def test_registrar_venta_with_description(db_session_factory):
    resp = _send(db_session_factory, "/registrar_venta 150000 | 3 tortas de chocolate")

    sales = _sales(db_session_factory)
    assert len(sales) == 1
    assert sales[0].total_amount == Decimal("150000.00")
    assert sales[0].description == "3 tortas de chocolate"
    assert sales[0].business_id == "biz-pedro-manual"
    assert sales[0].recorded_by_user_id == "usr-pedro-manual"
    assert sales[0].recorded_via == "TELEGRAM"
    assert "Venta registrada: $150,000 COP" in resp
    assert "3 tortas de chocolate" in resp


def test_registrar_venta_without_description(db_session_factory):
    resp = _send(db_session_factory, "/registrar_venta 150000")

    sales = _sales(db_session_factory)
    assert len(sales) == 1 and sales[0].description is None
    assert "Venta registrada: $150,000 COP" in resp
    assert "—" not in resp


def test_registrar_venta_shows_decimals_only_when_present(db_session_factory):
    resp = _send(db_session_factory, "/registrar_venta 100.50")

    assert "$100.50 COP" in resp
    assert _sales(db_session_factory)[0].total_amount == Decimal("100.50")


@pytest.mark.parametrize("text", ["/registrar_venta", "/registrar_venta abc", "/registrar_venta | solo descripción"])
def test_registrar_venta_missing_or_non_numeric_shows_help(db_session_factory, text):
    resp = _send(db_session_factory, text)

    assert "/registrar_venta <total>" in resp
    assert "Ejemplo" in resp
    assert _sales(db_session_factory) == []


@pytest.mark.parametrize("text", ["/registrar_venta $150000", "/registrar_venta 150,5", "/registrar_venta 150.000", "/registrar_venta 100.125"])
def test_registrar_venta_disallowed_format_shows_help(db_session_factory, text):
    resp = _send(db_session_factory, text)

    assert "Formato" in resp
    assert _sales(db_session_factory) == []


def test_registrar_venta_separator_edges(db_session_factory):
    _send(db_session_factory, "/registrar_venta 100 |")
    _send(db_session_factory, "/registrar_venta 100 | a | b")

    descriptions = sorted((s.description or "" for s in _sales(db_session_factory)))
    assert descriptions == ["", "a | b"]


def test_registrar_venta_description_over_500_is_rejected(db_session_factory):
    resp = _send(db_session_factory, "/registrar_venta 100 | " + "z" * 501)

    assert "500" in resp
    assert _sales(db_session_factory) == []


@pytest.mark.parametrize("text", ["/registrar_venta 0", "/registrar_venta -100"])
def test_registrar_venta_zero_or_negative_is_rejected(db_session_factory, text):
    resp = _send(db_session_factory, text)

    assert "mayor a cero" in resp
    assert _sales(db_session_factory) == []


def test_registrar_venta_blocked_business_gets_suspension_message(db_session_factory):
    resp = _send(db_session_factory, "/registrar_venta 100 | intento", chat_id=CARLOS_CHAT)

    assert "Servicio Suspendido" in resp
    assert _sales(db_session_factory) == []


def test_registrar_venta_unlinked_chat_gets_guard_message(db_session_factory):
    resp = _send(db_session_factory, "/registrar_venta 100", chat_id=424242)

    assert "Cuenta no vinculada" in resp
    assert _sales(db_session_factory) == []


def test_registrar_venta_preserves_description_case_and_is_command_case_insensitive(db_session_factory):
    _send(db_session_factory, "/REGISTRAR_VENTA 100 | Pedido de María ÑU")

    assert _sales(db_session_factory)[0].description == "Pedido de María ÑU"


def test_registrar_venta_escapes_markdown_in_confirmation(db_session_factory):
    resp = _send(db_session_factory, "/registrar_venta 100 | torta_grande *2* `x` [a]")

    assert r"torta\_grande \*2\* \`x\` \[a]" in resp
    assert _sales(db_session_factory)[0].description == "torta_grande *2* `x` [a]"


def test_registrar_venta_delegates_validation_to_sales_service(db_session_factory, monkeypatch):
    from dian_automation.core import sales_service

    calls = []

    def fake_register_sale(db, **kwargs):
        calls.append(kwargs)
        raise sales_service.SalesError(sales_service.SalesError.INVALID_TOTAL, "rechazado por el servicio")

    monkeypatch.setattr(sales_service, "register_sale", fake_register_sale)

    resp = _send(db_session_factory, "/registrar_venta 12abc | algo")

    assert len(calls) == 1
    assert calls[0]["total"] == "12abc" and calls[0]["description"] == " algo"
    assert calls[0]["recorded_via"] == "TELEGRAM"
    assert "/registrar_venta <total>" in resp
    assert _sales(db_session_factory) == []


def test_parse_registrar_venta_command_only_splits_text():
    parse = ClientTelegramBot.parse_registrar_venta_command

    assert parse("/registrar_venta 150000 | 3 tortas") == ("150000", " 3 tortas")
    assert parse("/registrar_venta 150000") == ("150000", None)
    assert parse("/registrar_venta") == ("", None)
    assert parse("/registrar_venta 100 | a | b") == ("100", " a | b")
    assert parse("/registrar_venta@KontableBot 100") == ("100", None)


def test_help_and_unknown_command_list_registrar_venta(db_session_factory):
    assert "/registrar_venta" in _send(db_session_factory, "/ayuda", chat_id=MANUAL_CHAT)
    assert "/registrar_venta" in _send(db_session_factory, "/comando_invalido", chat_id=MANUAL_CHAT)
    # Negocio DIAN no lista /registrar_venta en /ayuda
    assert "/registrar_venta" not in _send(db_session_factory, "/ayuda", chat_id=ANDREA_CHAT)


def test_registrar_venta_accepts_bot_name_suffix_in_router(db_session_factory):
    resp = _send(db_session_factory, "/registrar_venta@KontableBot 100 | grupo")

    assert "Venta registrada: $100 COP" in resp
    assert _sales(db_session_factory)[0].description == "grupo"


def test_registrar_venta_maps_service_block_error_to_suspension_message(db_session_factory, monkeypatch):
    """Carrera entre el guardia y el servicio: el bot traduce el error de bloqueo al aviso existente."""
    from dian_automation.core import sales_service
    from dian_automation.subscriptions.lockout_service import SubscriptionLockoutService

    def fake_register_sale(db, **kwargs):
        raise sales_service.SalesError(sales_service.SalesError.BUSINESS_BLOCKED, "bloqueado")

    monkeypatch.setattr(sales_service, "register_sale", fake_register_sale)

    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_registrar_venta(MANUAL_CHAT, db, "/registrar_venta 100")
    finally:
        db.close()

    assert resp["success"] is False
    assert resp["message"] == SubscriptionLockoutService.BLOCKED_TELEGRAM_MESSAGE


def test_registrar_venta_dian_business_returns_informational_message_and_no_sale(db_session_factory):
    """/registrar_venta con negocio DIAN responde con mensaje informativo y no crea venta."""
    resp = _send(db_session_factory, "/registrar_venta 100000", chat_id=ANDREA_CHAT)

    assert "ℹ️ Tu negocio factura electrónicamente: las ventas salen de la DIAN y no se registran a mano." in resp
    assert _sales(db_session_factory) == []


def test_client_resumen_manual_sales(db_session_factory):
    """/resumen MANUAL_SALES muestra ingresos/egresos/utilidad y no contiene 'IVA' ni 'impuesto'."""
    from dian_automation.db.models import Sale, Invoice
    db = db_session_factory()
    try:
        today = date.today()
        current_month = today.strftime("%Y-%m")
        sale = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("1500000.00"),
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=today,
            created_at=datetime.utcnow(),
        )
        inv = Invoice(
            id="inv-manual-egreso-1",
            business_id="biz-pedro-manual",
            cufe="cufe-egreso-1",
            document_type="Factura electrónica de venta",
            prefix="REC",
            folio="501",
            issue_date=datetime.utcnow(),
            issuer_nit="800111222",
            issuer_name="Proveedor Harina",
            receiver_nit="900888777",
            receiver_name="Panadería Pedro Gómez SAS",
            iva=Decimal("19000.00"),
            total=Decimal("200000.00"),
            group_type="Recibido",
        )
        db.add_all([sale, inv])
        db.commit()

        resp = ClientTelegramBot.handle_resumen(sender_chat_id=MANUAL_CHAT, db=db)
        assert resp["success"] is True
        assert resp["has_data"] is True

        msg = resp["message"]
        assert "Resumen del mes - Panadería Pedro" in msg
        assert current_month in msg
        assert "Ingresos" in msg
        assert "$1,500,000 COP" in msg
        assert "Egresos" in msg
        assert "$200,000 COP" in msg
        assert "Utilidad estimada" in msg
        assert "$1,300,000 COP" in msg
        assert "egresos corresponden al total de tus facturas recibidas" in msg

        # Verificación estricta: NO debe contener 'IVA' ni 'impuesto'
        assert "iva" not in msg.lower()
        assert "impuesto" not in msg.lower()
    finally:
        db.close()


def test_client_resumen_manual_sales_zeros_when_no_movement(db_session_factory):
    """/resumen MANUAL_SALES muestra ceros si el mes no tiene movimientos."""
    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_resumen(sender_chat_id=MANUAL_CHAT, db=db, target_period="2023-01")
        assert resp["success"] is True
        msg = resp["message"]
        assert "Resumen del mes - Panadería Pedro" in msg
        assert "2023-01" in msg
        assert "$0 COP" in msg
        assert "iva" not in msg.lower()
        assert "impuesto" not in msg.lower()
    finally:
        db.close()


def test_client_resumen_manual_sales_invalid_month_does_not_crash(db_session_factory):
    """/resumen 2026-13 (pasa el patrón AAAA-MM del enrutador) responde un mensaje claro, sin excepción."""
    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_resumen(sender_chat_id=MANUAL_CHAT, db=db, target_period="2026-13")
        assert resp["success"] is False
        assert resp["reason"] == "INVALID_MONTH"
        assert "/resumen AAAA-MM" in resp["message"]

        text = ClientTelegramBot.handle_client_message(MANUAL_CHAT, "/resumen 2026-13", db)
        assert "Mes inválido" in text
    finally:
        db.close()


def test_client_vencimientos_manual_sales(db_session_factory):
    """/vencimientos MANUAL_SALES responde indicando que no aplica con count 0."""
    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_vencimientos(sender_chat_id=MANUAL_CHAT, db=db)
        assert resp["success"] is True
        assert resp["count"] == 0
        assert "no aplica a tu tipo de negocio" in resp["message"]
    finally:
        db.close()


def test_client_help_by_business_type(db_session_factory):
    """/ayuda adapta sus opciones según el tipo de negocio."""
    db = db_session_factory()
    try:
        help_manual = ClientTelegramBot.handle_help(sender_chat_id=MANUAL_CHAT, db=db)
        assert "/resumen" in help_manual
        assert "/facturas" not in help_manual
        assert "/dashboard" in help_manual
        assert "/registrar_venta" in help_manual
        assert "/ayuda" in help_manual
        assert "/vencimientos" not in help_manual
        assert "iva" not in help_manual.lower()
        assert "impuesto" not in help_manual.lower()
        assert "datos provienen directamente del repositorio oficial de la DIAN" not in help_manual

        help_dian = ClientTelegramBot.handle_help(sender_chat_id=ANDREA_CHAT, db=db)
        assert "/resumen" in help_dian
        assert "/facturas" in help_dian
        assert "/vencimientos" in help_dian
        assert "/dashboard" in help_dian
        assert "/ayuda" in help_dian
        assert "/registrar_venta" not in help_dian
        assert "datos provienen directamente del repositorio oficial de la DIAN" in help_dian
    finally:
        db.close()


def test_dian_resumen_and_vencimientos_unaffected(db_session_factory):
    """Para negocio DIAN, /resumen y /vencimientos conservan su comportamiento original."""
    db = db_session_factory()
    try:
        summary = MonthlyTaxSummary(
            business_id="biz-andrea",
            period_year_month="2026-08",
            total_invoiced_net=Decimal("10000000.00"),
            iva_generado=Decimal("1900000.00"),
            iva_descontable=Decimal("500000.00"),
            iva_balance=Decimal("1400000.00"),
            rete_iva_total=Decimal("0.00"),
            rete_renta_total=Decimal("0.00"),
            rete_ica_total=Decimal("0.00"),
            total_invoices_count=5,
            variation_vs_previous_pct=Decimal("10.00"),
        )
        db.add(summary)
        db.commit()

        # DIAN /resumen incluye IVA
        res_dian = ClientTelegramBot.handle_resumen(sender_chat_id=ANDREA_CHAT, db=db, target_period="2026-08")
        assert res_dian["success"] is True
        assert "LIQUIDACIÓN DE IVA" in res_dian["message"]
        assert "Saldo a Pagar DIAN" in res_dian["message"]

        # DIAN /vencimientos incluye obligaciones del calendario
        venc_dian = ClientTelegramBot.handle_vencimientos(sender_chat_id=ANDREA_CHAT, db=db)
        assert venc_dian["success"] is True
        assert venc_dian["count"] > 0
        assert "CALENDARIO DE VENCIMIENTOS DIAN" in venc_dian["message"]
    finally:
        db.close()

