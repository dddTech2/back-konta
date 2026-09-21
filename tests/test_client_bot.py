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
        iva_periodicity="BIMESTRAL",
        is_withholding_agent=True,
        taxpayer_type="PERSONA_JURIDICA",
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
    curr_year = date.today().year
    cal_iva_9 = DIANTaxCalendar(
        tax_type="IVA_BIMESTRAL",
        fiscal_year=curr_year,
        period_label=f"Jul – Ago {curr_year}",
        period_start=date(curr_year, 7, 1),
        period_end=date(curr_year, 8, 31),
        key_length=1,
        key_from=9,
        key_to=9,
        installment=0,
        nit_last_digit=9,
        deadline_date=date.today() + timedelta(days=4),
        description="Declaración bimestral formulario 300",
    )
    cal_rete_9 = DIANTaxCalendar(
        tax_type="RETEFUENTE",
        fiscal_year=curr_year,
        period_label=f"Agosto {curr_year}",
        period_start=date(curr_year, 8, 1),
        period_end=date(curr_year, 8, 31),
        key_length=1,
        key_from=9,
        key_to=9,
        installment=0,
        nit_last_digit=9,
        deadline_date=date.today() + timedelta(days=12),
        description="Declaración mensual formulario 350",
    )
    cal_iva_4 = DIANTaxCalendar(
        tax_type="IVA_BIMESTRAL",
        fiscal_year=curr_year,
        period_label=f"Jul – Ago {curr_year}",
        period_start=date(curr_year, 7, 1),
        period_end=date(curr_year, 8, 31),
        key_length=1,
        key_from=4,
        key_to=4,
        installment=0,
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


def test_client_facturas_manual_sales_not_applicable(db_session_factory):
    """El comando /facturas para MANUAL_SALES devuelve mensaje de no aplicabilidad con count 0 sin consultar Invoice."""
    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_facturas(sender_chat_id=MANUAL_CHAT, db=db)
        assert resp["success"] is True
        assert resp["count"] == 0
        assert "ℹ️ /facturas lista las facturas electrónicas emitidas y no aplica a tu tipo de negocio." in resp["message"]
        assert "Usa /resumen para ver tus ingresos y egresos, o /mis_ventas para tus ventas." in resp["message"]

        router_resp = ClientTelegramBot.handle_client_message(sender_chat_id=MANUAL_CHAT, text="/facturas", db=db)
        assert "no aplica a tu tipo de negocio" in router_resp
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
        assert "IVA_BIMESTRAL" in msg
        assert "RETEFUENTE" in msg
        assert f"Agosto {date.today().year}" in msg
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
        assert "¡Hola, bienvenido a Konta Bot!" in help_resp
        assert "/resumen" in help_resp
        assert "/facturas" in help_resp
        assert "/vencimientos" in help_resp
        assert "/dashboard" in help_resp

        start_resp = ClientTelegramBot.handle_client_message(
            sender_chat_id=777888999, text="/start", db=db
        )
        assert "/resumen" in start_resp

        # Mensaje no reconocido para negocio DIAN
        unknown_resp = ClientTelegramBot.handle_client_message(
            sender_chat_id=777888999, text="/comando_invalido", db=db
        )
        assert "🤖 No reconozco ese comando." in unknown_resp
        assert "• /resumen — Resumen fiscal del mes e IVA" in unknown_resp
        assert "• /facturas — Últimas 4 facturas emitidas" in unknown_resp
        assert "• /vencimientos — Calendario de impuestos DIAN" in unknown_resp
        assert "• /dashboard — Enlace a tu panel web/móvil" in unknown_resp
        assert "• /ayuda — Menú de ayuda" in unknown_resp
        assert "/registrar_venta" not in unknown_resp
        assert "/mis_ventas" not in unknown_resp
        assert "/anular_venta" not in unknown_resp

        # Mensaje no reconocido para negocio MANUAL_SALES
        unknown_manual = ClientTelegramBot.handle_client_message(
            sender_chat_id=555666777, text="/comando_invalido", db=db
        )
        assert "🤖 No reconozco ese comando." in unknown_manual
        assert "• /resumen — Ingresos, egresos y utilidad del mes" in unknown_manual
        assert "• /dashboard — Enlace a tu panel web/móvil" in unknown_manual
        assert "• /registrar_venta — Registra una venta (total y descripción opcional)" in unknown_manual
        assert "• /mis_ventas — Tus últimas 10 ventas" in unknown_manual
        assert "• /anular_venta — Anula una venta mal registrada" in unknown_manual
        assert "• /ayuda — Menú de ayuda" in unknown_manual
        assert "/facturas" not in unknown_manual
        assert "/vencimientos" not in unknown_manual
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


def test_client_vencimientos_message_formatting(db_session_factory):
    """El mensaje de /vencimientos conserva idéntico formato de líneas, iconos y estados."""
    db = db_session_factory()
    try:
        ref_date = date(2026, 9, 15)
        # Limpiar calendario previo y sembrar casos específicos para probar todos los estados
        db.query(DIANTaxCalendar).delete()

        entries = [
            DIANTaxCalendar(
                tax_type="IVA_BIMESTRAL",
                fiscal_year=2026,
                period_label="Bimestre 1",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 2, 28),
                key_length=1,
                key_from=9,
                key_to=9,
                installment=0,
                deadline_date=ref_date - timedelta(days=3),
                description="Venció en el pasado",
            ),
            DIANTaxCalendar(
                tax_type="IVA_BIMESTRAL",
                fiscal_year=2026,
                period_label="Bimestre 2",
                period_start=date(2026, 3, 1),
                period_end=date(2026, 4, 30),
                key_length=1,
                key_from=9,
                key_to=9,
                installment=0,
                deadline_date=ref_date,
                description="Vence el día de hoy",
            ),
            DIANTaxCalendar(
                tax_type="IVA_BIMESTRAL",
                fiscal_year=2026,
                period_label="Bimestre 3",
                period_start=date(2026, 5, 1),
                period_end=date(2026, 6, 30),
                key_length=1,
                key_from=9,
                key_to=9,
                installment=0,
                deadline_date=ref_date + timedelta(days=2),
                description="Vence pronto en 2 días",
            ),
            DIANTaxCalendar(
                tax_type="IVA_BIMESTRAL",
                fiscal_year=2026,
                period_label="Bimestre 4",
                period_start=date(2026, 7, 1),
                period_end=date(2026, 8, 31),
                key_length=1,
                key_from=9,
                key_to=9,
                installment=0,
                deadline_date=ref_date + timedelta(days=10),
                description="Vence en 10 días",
            ),
        ]
        db.add_all(entries)
        db.commit()

        resp = ClientTelegramBot.handle_vencimientos(
            sender_chat_id=ANDREA_CHAT, db=db, reference_date=ref_date
        )
        assert resp["success"] is True
        assert resp["count"] == 4

        msg = resp["message"]
        assert "CALENDARIO DE VENCIMIENTOS DIAN" in msg
        assert "Fecha Límite:" in msg
        assert "⚠️ Venció hace 3 días" in msg
        assert "🚨 ¡VENCE HOY!" in msg
        assert "⏳ En 2 días" in msg
        assert "📅 En 10 días" in msg
    finally:
        db.close()


def test_client_vencimientos_calendar_not_loaded(db_session_factory):
    """Si el calendario del año no está cargado retorna CALENDAR_NOT_LOADED con mensaje amigable."""
    db = db_session_factory()
    try:
        # Consultar para un año sin calendario cargado (2035)
        resp = ClientTelegramBot.handle_vencimientos(
            sender_chat_id=ANDREA_CHAT, db=db, reference_date=date(2035, 1, 1)
        )
        assert resp["success"] is False
        assert resp["reason"] == "CALENDAR_NOT_LOADED"
        assert resp["message"] == (
            "🗓️ *Calendario Tributario DIAN*\n\n"
            "ℹ️ El calendario tributario de este año aún no está cargado. Inténtalo de nuevo más tarde."
        )
    finally:
        db.close()


def test_client_vencimientos_filtered_by_business_profile(db_session_factory):
    """Obligaciones se filtran por perfil del negocio (no aparece RETEFUENTE si no es agente retenedor)."""
    db = db_session_factory()
    try:
        biz = db.query(Business).filter(Business.id == "biz-andrea").first()
        biz.is_withholding_agent = False
        db.commit()

        resp = ClientTelegramBot.handle_vencimientos(
            sender_chat_id=ANDREA_CHAT, db=db, reference_date=date.today()
        )
        assert resp["success"] is True
        msg = resp["message"]
        assert "IVA_BIMESTRAL" in msg
        assert "RETEFUENTE" not in msg
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Story 6.5: /mis_ventas y /anular_venta (Parte B2)
# ---------------------------------------------------------------------------


def test_parse_anular_venta_command():
    """Valida la extracción del número entero positivo o None según la sintaxis."""
    parse = ClientTelegramBot.parse_anular_venta_command

    assert parse("/anular_venta 1") == 1
    assert parse("/anular_venta 10") == 10
    assert parse("/anular_venta@KontableBot 5") == 5
    assert parse("/ANULAR_VENTA 3") == 3
    assert parse("/anular_venta") is None
    assert parse("/anular_venta 0") is None
    assert parse("/anular_venta -1") is None
    assert parse("/anular_venta abc") is None
    assert parse("/anular_venta 1 2") is None
    assert parse("/anular_venta 1.5") is None
    assert parse("hola") is None
    assert parse("") is None


def test_mis_ventas_listing_with_details(db_session_factory):
    """/mis_ventas lista con número, fecha dd/mm/AAAA, monto y descripción."""
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        sale1 = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("50000.00"),
            description="Pan integral y café",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 10),
            created_at=datetime(2026, 9, 10, 8, 0, 0),
        )
        sale2 = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("120000.00"),
            description=None,
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 12),
            created_at=datetime(2026, 9, 12, 10, 30, 0),
        )
        db.add_all([sale1, sale2])
        db.commit()

        resp = ClientTelegramBot.handle_mis_ventas(sender_chat_id=MANUAL_CHAT, db=db)
        assert resp["success"] is True
        assert resp["count"] == 2

        msg = resp["message"]
        assert "🧾 *Tus últimas ventas*" in msg
        # La más reciente primero (sale2 es del 12/09 con número estable 2, sale1 del 10/09 con número estable 1)
        assert "2. 12/09/2026 — $120,000 COP" in msg
        # Sin descripción: se omite el guion y la descripción
        assert "2. 12/09/2026 — $120,000 COP —" not in msg
        # Con descripción
        assert "1. 10/09/2026 — $50,000 COP — Pan integral y café" in msg
        assert "Para anular una: `/anular_venta 2` (el número de cada venta no cambia)." in msg
    finally:
        db.close()


def test_mis_ventas_max_10_even_if_12_sales(db_session_factory):
    """/mis_ventas muestra máximo 10 aunque haya 12 ventas y la más reciente es la 12."""
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        base_date = date(2026, 9, 1)
        base_time = datetime(2026, 9, 1, 8, 0, 0)
        for i in range(1, 13):
            sale = Sale(
                business_id="biz-pedro-manual",
                total_amount=Decimal(f"{i * 10000}.00"),
                description=f"Venta número {i}",
                recorded_via="TELEGRAM",
                recorded_by_user_id="usr-pedro-manual",
                sale_date=base_date + timedelta(days=i),
                created_at=base_time + timedelta(hours=i),
            )
            db.add(sale)
        db.commit()

        resp = ClientTelegramBot.handle_mis_ventas(sender_chat_id=MANUAL_CHAT, db=db)
        assert resp["success"] is True
        assert resp["count"] == 10

        msg = resp["message"]
        # La más reciente es la 12, con su número estable 12
        assert "12. 13/09/2026 — $120,000 COP — Venta número 12" in msg
        # La más antigua de las 10 recientes es la venta 3 con su número estable 3
        assert "3. 04/09/2026 — $30,000 COP — Venta número 3" in msg
        # El pie incluye el ejemplo con la primera venta de la lista (12)
        assert "Para anular una: `/anular_venta 12` (el número de cada venta no cambia)." in msg
        # Las ventas 1 y 2 quedaron fuera del límite de 10
        assert "Venta número 1\n" not in msg and not msg.endswith("Venta número 1")
        assert "Venta número 2" not in msg
        assert not any(line.startswith(("1. ", "2. ")) for line in msg.splitlines())
    finally:
        db.close()


def test_mis_ventas_excludes_voided_sales(db_session_factory):
    """/mis_ventas excluye ventas anuladas del listado."""
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        sale1 = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("40000.00"),
            description="Venta activa 1",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 10),
            created_at=datetime(2026, 9, 10, 8, 0, 0),
        )
        sale2 = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("60000.00"),
            description="Venta a anular",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 11),
            created_at=datetime(2026, 9, 11, 8, 0, 0),
        )
        sale3 = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("80000.00"),
            description="Venta activa 2",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 12),
            created_at=datetime(2026, 9, 12, 8, 0, 0),
        )
        db.add_all([sale1, sale2, sale3])
        db.commit()

        # Anular la venta con número estable 2
        res_void = ClientTelegramBot.handle_anular_venta(
            sender_chat_id=MANUAL_CHAT, db=db, text="/anular_venta 2"
        )
        assert res_void["success"] is True

        resp = ClientTelegramBot.handle_mis_ventas(sender_chat_id=MANUAL_CHAT, db=db)
        assert resp["success"] is True
        assert resp["count"] == 2

        msg = resp["message"]
        assert "3. 12/09/2026 — $80,000 COP — Venta activa 2" in msg
        assert "1. 10/09/2026 — $40,000 COP — Venta activa 1" in msg
        assert "Venta a anular" not in msg
        assert "Para anular una: `/anular_venta 3` (el número de cada venta no cambia)." in msg
    finally:
        db.close()


def test_mis_ventas_empty_when_no_sales(db_session_factory):
    """/mis_ventas responde mensaje amigable cuando aún no hay ventas."""
    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_mis_ventas(sender_chat_id=MANUAL_CHAT, db=db)
        assert resp["success"] is True
        assert resp["count"] == 0
        assert resp["message"] == "ℹ️ Aún no tienes ventas registradas."
    finally:
        db.close()


def test_mis_ventas_dian_business_not_applicable(db_session_factory):
    """/mis_ventas con negocio DIAN devuelve mensaje de no aplicabilidad."""
    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_mis_ventas(sender_chat_id=ANDREA_CHAT, db=db)
        assert resp["success"] is False
        assert resp["reason"] == "NOT_MANUAL_SALES"
        assert "ℹ️ Tu negocio factura electrónicamente: las ventas salen de la DIAN y no se registran a mano." in resp["message"]
    finally:
        db.close()


def test_mis_ventas_subscription_blocked(db_session_factory):
    """/mis_ventas con cliente BLOQUEADO devuelve mensaje de suscripción suspendida."""
    db = db_session_factory()
    try:
        resp = ClientTelegramBot.handle_mis_ventas(sender_chat_id=CARLOS_CHAT, db=db)
        assert resp["success"] is False
        assert resp["reason"] == "SUBSCRIPTION_BLOCKED"
        assert "Servicio Suspendido" in resp["message"]

        # A través del enrutador
        router_resp = _send(db_session_factory, "/mis_ventas", chat_id=CARLOS_CHAT)
        assert "Servicio Suspendido" in router_resp
    finally:
        db.close()


def test_anular_venta_position_2_success(db_session_factory):
    """/anular_venta 2 anula la segunda de la lista, actualiza voided_at y no aparece en /mis_ventas."""
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        base_time = datetime(2026, 9, 20, 10, 0, 0)
        sale1 = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("15000.00"),
            description="Venta vieja",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 18),
            created_at=base_time - timedelta(days=2),
        )
        sale2 = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("25000.00"),
            description="Venta del medio",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 19),
            created_at=base_time - timedelta(days=1),
        )
        sale3 = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("35000.00"),
            description="Venta reciente",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 20),
            created_at=base_time,
        )
        db.add_all([sale1, sale2, sale3])
        db.commit()

        # Orden reciente: 1=sale3, 2=sale2, 3=sale1
        resp = ClientTelegramBot.handle_anular_venta(
            sender_chat_id=MANUAL_CHAT, db=db, text="/anular_venta 2"
        )
        assert resp["success"] is True
        assert resp["sale_id"] == sale2.id

        msg = resp["message"]
        assert "🗑️ Venta anulada:" in msg
        assert "$25,000 COP" in msg
        assert "Venta del medio" in msg
        assert "19/09/2026" in msg

        # Verificar que la fila sigue existiendo en DB y tiene voided_at y voided_by_user_id
        all_sales = db.query(Sale).all()
        assert len(all_sales) == 3

        db.refresh(sale2)
        assert sale2.voided_at is not None
        assert sale2.voided_by_user_id == "usr-pedro-manual"

        db.refresh(sale1)
        assert sale1.voided_at is None
        db.refresh(sale3)
        assert sale3.voided_at is None

        # /mis_ventas ya no muestra la venta anulada y muestra números estables
        list_resp = ClientTelegramBot.handle_mis_ventas(sender_chat_id=MANUAL_CHAT, db=db)
        assert list_resp["success"] is True
        assert list_resp["count"] == 2
        assert "Venta del medio" not in list_resp["message"]
        assert "3. 20/09/2026 — $35,000 COP — Venta reciente" in list_resp["message"]
        assert "1. 18/09/2026 — $15,000 COP — Venta vieja" in list_resp["message"]
        assert "Para anular una: `/anular_venta 3` (el número de cada venta no cambia)." in list_resp["message"]
    finally:
        db.close()


def test_anular_venta_without_description(db_session_factory):
    """/anular_venta de una venta sin descripción omite ese tramo en el mensaje de éxito."""
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        sale = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("50000.00"),
            description=None,
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 20),
            created_at=datetime(2026, 9, 20, 10, 0, 0),
        )
        db.add(sale)
        db.commit()

        resp = ClientTelegramBot.handle_anular_venta(
            sender_chat_id=MANUAL_CHAT, db=db, text="/anular_venta 1"
        )
        assert resp["success"] is True
        assert resp["message"] == "🗑️ Venta anulada: $50,000 COP (20/09/2026)"
    finally:
        db.close()


@pytest.mark.parametrize(
    "cmd_text",
    [
        "/anular_venta",
        "/anular_venta 0",
        "/anular_venta -1",
        "/anular_venta -5",
        "/anular_venta abc",
        "/anular_venta 1 2",
        "/anular_venta 1.5",
        "/anular_venta $2",
    ],
)
def test_anular_venta_invalid_numbers_error_without_voiding(db_session_factory, cmd_text):
    """Número 0, negativo, no numérico o ausente retorna INVALID_NUMBER sin anular nada."""
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        sale = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("70000.00"),
            description="Venta intacta",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 20),
            created_at=datetime(2026, 9, 20, 10, 0, 0),
        )
        db.add(sale)
        db.commit()

        resp = ClientTelegramBot.handle_anular_venta(
            sender_chat_id=MANUAL_CHAT, db=db, text=cmd_text
        )
        assert resp["success"] is False
        assert resp["reason"] == "INVALID_NUMBER"
        assert "❌ Indica el número de la venta a anular, por ejemplo `/anular_venta 2`." in resp["message"]
        assert "Mira tu lista con /mis_ventas." in resp["message"]

        db.refresh(sale)
        assert sale.voided_at is None
    finally:
        db.close()


@pytest.mark.parametrize("cmd_text", ["/anular_venta 3", "/anular_venta 99"])
def test_anular_venta_number_out_of_range(db_session_factory, cmd_text):
    """Número fuera de rango (mayor a len(recent)) retorna NUMBER_OUT_OF_RANGE sin anular nada."""
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        sale1 = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("30000.00"),
            description="Venta 1",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 20),
            created_at=datetime(2026, 9, 20, 10, 0, 0),
        )
        sale2 = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("40000.00"),
            description="Venta 2",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 20),
            created_at=datetime(2026, 9, 20, 11, 0, 0),
        )
        db.add_all([sale1, sale2])
        db.commit()

        resp = ClientTelegramBot.handle_anular_venta(
            sender_chat_id=MANUAL_CHAT, db=db, text=cmd_text
        )
        assert resp["success"] is False
        assert resp["reason"] == "NUMBER_OUT_OF_RANGE"
        assert "❌ Ese número no está en tu lista actual (o la venta ya está anulada). Usa /mis_ventas para ver las ventas que puedes anular." in resp["message"]

        db.refresh(sale1)
        db.refresh(sale2)
        assert sale1.voided_at is None
        assert sale2.voided_at is None
    finally:
        db.close()


def test_anular_dos_veces_mismo_numero_no_anula_otra(db_session_factory):
    """Anular dos veces con el mismo número responde NUMBER_OUT_OF_RANGE y NO anula ninguna otra."""
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        base_time = datetime(2026, 9, 20, 10, 0, 0)
        sale_a = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("10000.00"),
            description="Venta A",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 18),
            created_at=base_time - timedelta(hours=2),
        )
        sale_b = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("20000.00"),
            description="Venta B",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 19),
            created_at=base_time - timedelta(hours=1),
        )
        sale_c = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("30000.00"),
            description="Venta C",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 20),
            created_at=base_time,
        )
        db.add_all([sale_a, sale_b, sale_c])
        db.commit()

        # Números estables: sale_a=1, sale_b=2, sale_c=3
        # Primera anulación para número 1 (Venta A): éxito
        resp1 = ClientTelegramBot.handle_anular_venta(
            sender_chat_id=MANUAL_CHAT, db=db, text="/anular_venta 1"
        )
        assert resp1["success"] is True
        assert resp1["sale_id"] == sale_a.id
        assert "Venta A" in resp1["message"]

        db.refresh(sale_a)
        assert sale_a.voided_at is not None

        # Segunda anulación con el MISMO número 1: venta ya anulada / fuera de rango, no anula ninguna otra
        resp2 = ClientTelegramBot.handle_anular_venta(
            sender_chat_id=MANUAL_CHAT, db=db, text="/anular_venta 1"
        )
        assert resp2["success"] is False
        assert resp2["reason"] == "NUMBER_OUT_OF_RANGE"
        assert "❌ Ese número no está en tu lista actual (o la venta ya está anulada). Usa /mis_ventas para ver las ventas que puedes anular." in resp2["message"]

        # Las ventas B y C siguen activas e intactas
        db.refresh(sale_b)
        assert sale_b.voided_at is None
        db.refresh(sale_c)
        assert sale_c.voided_at is None

        # /mis_ventas conserva a C (3) y B (2) con sus números estables
        list_resp = ClientTelegramBot.handle_mis_ventas(sender_chat_id=MANUAL_CHAT, db=db)
        assert list_resp["count"] == 2
        assert "3. 20/09/2026 — $30,000 COP — Venta C" in list_resp["message"]
        assert "2. 19/09/2026 — $20,000 COP — Venta B" in list_resp["message"]
    finally:
        db.close()


def test_registrar_venta_entre_mis_ventas_y_anular_venta_mantiene_numero(db_session_factory):
    """Tras registrar otra venta entre /mis_ventas y /anular_venta, el número mostrado sigue anulando la MISMA venta."""
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        base_time = datetime(2026, 9, 20, 10, 0, 0)
        sale_primera = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("15000.00"),
            description="Primera venta original",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 20),
            created_at=base_time,
        )
        db.add(sale_primera)
        db.commit()

        # El cliente consulta /mis_ventas y ve la venta con número estable 1
        list_resp = ClientTelegramBot.handle_mis_ventas(sender_chat_id=MANUAL_CHAT, db=db)
        assert list_resp["count"] == 1
        assert "1. 20/09/2026 — $15,000 COP — Primera venta original" in list_resp["message"]
        assert "Para anular una: `/anular_venta 1`" in list_resp["message"]

        # En el medio, se registra otra venta (obtiene número estable 2)
        sale_segunda = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("50000.00"),
            description="Segunda venta posterior",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 20),
            created_at=base_time + timedelta(minutes=5),
        )
        db.add(sale_segunda)
        db.commit()

        # El cliente ejecuta /anular_venta 1 según el número que vio en /mis_ventas
        void_resp = ClientTelegramBot.handle_anular_venta(
            sender_chat_id=MANUAL_CHAT, db=db, text="/anular_venta 1"
        )
        assert void_resp["success"] is True
        assert void_resp["sale_id"] == sale_primera.id
        assert "Primera venta original" in void_resp["message"]

        # La primera venta fue anulada; la segunda permanece activa
        db.refresh(sale_primera)
        assert sale_primera.voided_at is not None

        db.refresh(sale_segunda)
        assert sale_segunda.voided_at is None
    finally:
        db.close()


def test_anular_venta_dian_and_blocked_business_no_void(db_session_factory):
    """Negocio DIAN y cliente BLOQUEADO son rechazados y no anulan ventas."""
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        # Negocio DIAN
        resp_dian = ClientTelegramBot.handle_anular_venta(
            sender_chat_id=ANDREA_CHAT, db=db, text="/anular_venta 1"
        )
        assert resp_dian["success"] is False
        assert resp_dian["reason"] == "NOT_MANUAL_SALES"
        assert "ℹ️ Tu negocio factura electrónicamente: las ventas salen de la DIAN y no se registran a mano." in resp_dian["message"]

        # Cliente BLOQUEADO
        resp_bloq = ClientTelegramBot.handle_anular_venta(
            sender_chat_id=CARLOS_CHAT, db=db, text="/anular_venta 1"
        )
        assert resp_bloq["success"] is False
        assert resp_bloq["reason"] == "SUBSCRIPTION_BLOCKED"
        assert "Servicio Suspendido" in resp_bloq["message"]

        # A través del router
        router_bloq = _send(db_session_factory, "/anular_venta 1", chat_id=CARLOS_CHAT)
        assert "Servicio Suspendido" in router_bloq
    finally:
        db.close()


def test_anular_venta_service_sales_error_mapping(db_session_factory, monkeypatch):
    """handle_anular_venta mapea adecuadamente los errores tipados de SalesError."""
    from dian_automation.core import sales_service
    from dian_automation.subscriptions.lockout_service import SubscriptionLockoutService
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        sale = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("50000.00"),
            description="Venta prueba",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 20),
            created_at=datetime(2026, 9, 20, 10, 0, 0),
        )
        db.add(sale)
        db.commit()

        # 1. Error BUSINESS_BLOCKED
        def fake_void_blocked(*args, **kwargs):
            raise sales_service.SalesError(sales_service.SalesError.BUSINESS_BLOCKED, "bloqueado")

        monkeypatch.setattr(sales_service, "void_sale", fake_void_blocked)
        resp1 = ClientTelegramBot.handle_anular_venta(MANUAL_CHAT, db, "/anular_venta 1")
        assert resp1["success"] is False
        assert resp1["reason"] == "BUSINESS_BLOCKED"
        assert resp1["message"] == SubscriptionLockoutService.BLOCKED_TELEGRAM_MESSAGE

        # 2. Error SALE_NOT_FOUND
        sale_not_found_code = getattr(sales_service.SalesError, "SALE_NOT_FOUND", "SALE_NOT_FOUND")

        def fake_void_not_found(*args, **kwargs):
            raise sales_service.SalesError(sale_not_found_code, "Venta no encontrada.")

        monkeypatch.setattr(sales_service, "void_sale", fake_void_not_found)
        resp2 = ClientTelegramBot.handle_anular_venta(MANUAL_CHAT, db, "/anular_venta 1")
        assert resp2["success"] is False
        assert resp2["reason"] == sale_not_found_code
        assert resp2["message"] == "❌ Venta no encontrada. Usa /mis_ventas para ver tu lista actual."

        # 3. Error ALREADY_VOIDED
        already_voided_code = getattr(sales_service.SalesError, "ALREADY_VOIDED", "ALREADY_VOIDED")

        def fake_void_already(*args, **kwargs):
            raise sales_service.SalesError(already_voided_code, "Esta venta ya fue anulada.")

        monkeypatch.setattr(sales_service, "void_sale", fake_void_already)
        resp3 = ClientTelegramBot.handle_anular_venta(MANUAL_CHAT, db, "/anular_venta 1")
        assert resp3["success"] is False
        assert resp3["reason"] == already_voided_code
        assert resp3["message"] == "❌ Esta venta ya fue anulada. Usa /mis_ventas para ver tu lista actual."
    finally:
        db.close()


def test_router_handles_mis_ventas_and_anular_venta_with_bot_suffix(db_session_factory):
    """handle_client_message responde a /mis_ventas, /anular_venta 1 y variantes con @bot."""
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        base_time = datetime(2026, 9, 20, 10, 0, 0)
        sale1 = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("30000.00"),
            description="Primera",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 20),
            created_at=base_time,
        )
        sale2 = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("40000.00"),
            description="Segunda",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 20),
            created_at=base_time + timedelta(minutes=5),
        )
        db.add_all([sale1, sale2])
        db.commit()
    finally:
        db.close()

    # /mis_ventas estándar
    resp_list = _send(db_session_factory, "/mis_ventas")
    assert "🧾 *Tus últimas ventas*" in resp_list
    assert "Segunda" in resp_list
    assert "Primera" in resp_list

    # /mis_ventas@KontableBot con sufijo
    resp_list_bot = _send(db_session_factory, "/mis_ventas@KontableBot")
    assert "🧾 *Tus últimas ventas*" in resp_list_bot

    # /anular_venta@KontableBot 2 (anula la segunda, que tiene el número estable 2)
    resp_void_bot = _send(db_session_factory, "/anular_venta@KontableBot 2")
    assert "🗑️ Venta anulada:" in resp_void_bot
    assert "Segunda" in resp_void_bot

    # /anular_venta 1 (anula la primera, que tiene el número estable 1)
    resp_void = _send(db_session_factory, "/anular_venta 1")
    assert "🗑️ Venta anulada:" in resp_void
    assert "Primera" in resp_void

    # Ya no quedan ventas activas
    resp_empty = _send(db_session_factory, "/mis_ventas")
    assert "ℹ️ Aún no tienes ventas registradas." in resp_empty


def test_help_menu_manual_sales_includes_mis_ventas_and_anular_venta(db_session_factory):
    """El menú de ayuda de MANUAL_SALES incluye ambos comandos y el de DIAN no."""
    db = db_session_factory()
    try:
        help_manual = ClientTelegramBot.handle_help(sender_chat_id=MANUAL_CHAT, db=db)
        assert "🧾 */mis_ventas* — Tus últimas 10 ventas, cada una con su número para anular." in help_manual
        assert "🗑️ */anular_venta* — Anula una venta mal registrada: `/anular_venta 2`." in help_manual

        help_dian = ClientTelegramBot.handle_help(sender_chat_id=ANDREA_CHAT, db=db)
        assert "/mis_ventas" not in help_dian
        assert "/anular_venta" not in help_dian
    finally:
        db.close()


def test_markdown_escaping_in_mis_ventas_and_anular_venta(db_session_factory):
    """La descripción con caracteres especiales de Markdown se escapa en /mis_ventas y /anular_venta."""
    from dian_automation.db.models import Sale

    db = db_session_factory()
    try:
        sale = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("100000.00"),
            description="torta_especial *3* `promo` [box]",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=date(2026, 9, 20),
            created_at=datetime(2026, 9, 20, 10, 0, 0),
        )
        db.add(sale)
        db.commit()

        # En /mis_ventas
        list_resp = ClientTelegramBot.handle_mis_ventas(sender_chat_id=MANUAL_CHAT, db=db)
        assert r"torta\_especial \*3\* \`promo\` \[box]" in list_resp["message"]

        # En /anular_venta
        void_resp = ClientTelegramBot.handle_anular_venta(
            sender_chat_id=MANUAL_CHAT, db=db, text="/anular_venta 1"
        )
        assert r"torta\_especial \*3\* \`promo\` \[box]" in void_resp["message"]
    finally:
        db.close()

