"""Pruebas del bot de Telegram para gastos, fecha del mes anterior y resumen (Story 11.2)."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.core import expense_service, sales_service
from dian_automation.core.calendar_engine import format_limit_date, today_bogota
from dian_automation.db.models import (
    Base,
    Business,
    Expense,
    Invoice,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    INCOME_SOURCE_MIXED,
    Sale,
    Subscription,
    User,
)
from dian_automation.telegram.client_bot import ClientTelegramBot

MANUAL_CHAT = 555666777
MIXED_CHAT = 333444555
DIAN_CHAT = 777888999
BLOCKED_CHAT = 111000111
UNLINKED_CHAT = 999999999


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria con perfiles para pruebas de Story 11.2."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = TestingSessionLocal()

    # 1. Cliente ventas manuales (Pedro Gómez)
    user_pedro = User(
        id="usr-pedro-manual",
        email="pedro@panaderia.co",
        full_name="Pedro Gómez",
        role="CLIENT",
        telegram_chat_id=MANUAL_CHAT,
        is_telegram_linked=True,
        is_active=True,
    )
    biz_pedro = Business(
        id="biz-pedro-manual",
        client_id=user_pedro.id,
        legal_name="Panadería Pedro Gómez SAS",
        commercial_name="Panadería Pedro",
        nit="900888777",
        dv="3",
        income_source=INCOME_SOURCE_MANUAL_SALES,
        is_active=True,
    )
    sub_pedro = Subscription(
        id="sub-pedro",
        client_id=user_pedro.id,
        plan="MENSUAL",
        discount_rate=Decimal("0.00"),
        base_price=Decimal("50000.00"),
        final_price=Decimal("50000.00"),
        start_date=date.today() - timedelta(days=10),
        cutoff_date=date.today() + timedelta(days=20),
        grace_period_end=date.today() + timedelta(days=23),
        status="ACTIVO",
    )

    # 2. Cliente mixto (Carlos Mixto)
    user_mixed = User(
        id="usr-carlos-mixed",
        email="carlos@mixto.co",
        full_name="Carlos Mixto",
        role="CLIENT",
        telegram_chat_id=MIXED_CHAT,
        is_telegram_linked=True,
        is_active=True,
    )
    biz_mixed = Business(
        id="biz-carlos-mixed",
        client_id=user_mixed.id,
        legal_name="Comercial Mixta SAS",
        commercial_name="Comercial Mixta",
        nit="901222333",
        dv="1",
        income_source=INCOME_SOURCE_MIXED,
        is_active=True,
    )
    sub_mixed = Subscription(
        id="sub-mixed",
        client_id=user_mixed.id,
        plan="MENSUAL",
        discount_rate=Decimal("0.00"),
        base_price=Decimal("50000.00"),
        final_price=Decimal("50000.00"),
        start_date=date.today() - timedelta(days=10),
        cutoff_date=date.today() + timedelta(days=20),
        grace_period_end=date.today() + timedelta(days=23),
        status="ACTIVO",
    )

    # 3. Cliente DIAN (Andrea Torres)
    user_dian = User(
        id="usr-andrea-dian",
        email="andrea@dian.co",
        full_name="Andrea Torres",
        role="CLIENT",
        telegram_chat_id=DIAN_CHAT,
        is_telegram_linked=True,
        is_active=True,
    )
    biz_dian = Business(
        id="biz-andrea-dian",
        client_id=user_dian.id,
        legal_name="Torres Studio SAS",
        commercial_name="Torres Studio",
        nit="901008579",
        dv="7",
        income_source=INCOME_SOURCE_DIAN,
        is_active=True,
    )
    sub_dian = Subscription(
        id="sub-dian",
        client_id=user_dian.id,
        plan="MENSUAL",
        discount_rate=Decimal("0.00"),
        base_price=Decimal("50000.00"),
        final_price=Decimal("50000.00"),
        start_date=date.today() - timedelta(days=10),
        cutoff_date=date.today() + timedelta(days=20),
        grace_period_end=date.today() + timedelta(days=23),
        status="ACTIVO",
    )

    # 4. Cliente con suscripción bloqueada
    user_bloq = User(
        id="usr-bloq",
        email="bloq@empresa.co",
        full_name="Bloqueado Mora",
        role="CLIENT",
        telegram_chat_id=BLOCKED_CHAT,
        is_telegram_linked=True,
        is_active=True,
    )
    biz_bloq = Business(
        id="biz-bloq",
        client_id=user_bloq.id,
        legal_name="Bloqueado SAS",
        commercial_name="Bloqueado Distribuciones",
        nit="800197268",
        dv="4",
        income_source=INCOME_SOURCE_MANUAL_SALES,
        is_active=True,
    )
    sub_bloq = Subscription(
        id="sub-bloq",
        client_id=user_bloq.id,
        plan="MENSUAL",
        discount_rate=Decimal("0.00"),
        base_price=Decimal("50000.00"),
        final_price=Decimal("50000.00"),
        start_date=date.today() - timedelta(days=60),
        cutoff_date=date.today() - timedelta(days=10),
        grace_period_end=date.today() - timedelta(days=7),
        status="BLOQUEADO",
    )

    db.add_all([
        user_pedro, biz_pedro, sub_pedro,
        user_mixed, biz_mixed, sub_mixed,
        user_dian, biz_dian, sub_dian,
        user_bloq, biz_bloq, sub_bloq,
    ])
    db.commit()
    db.close()

    def make_session():
        return TestingSessionLocal()

    return make_session


def _send(factory, text: str, chat_id: int = MANUAL_CHAT) -> str:
    db = factory()
    try:
        return ClientTelegramBot.handle_client_message(chat_id, text, db)
    finally:
        db.close()


def _expenses(factory):
    db = factory()
    try:
        return db.query(Expense).order_by(Expense.created_at.asc()).all()
    finally:
        db.close()


def _sales(factory):
    db = factory()
    try:
        return db.query(Sale).order_by(Sale.created_at.asc()).all()
    finally:
        db.close()


def _prev_month_date() -> date:
    """Devuelve un día válido del mes anterior (el 15 del mes anterior)."""
    today = today_bogota()
    if today.month == 1:
        return date(today.year - 1, 12, 15)
    return date(today.year, today.month - 1, 15)


def _out_of_range_date() -> date:
    """Devuelve una fecha de hace 3 meses (fuera de rango permitido)."""
    today = today_bogota()
    if today.month <= 3:
        return date(today.year - 1, today.month + 9, 1)
    return date(today.year, today.month - 3, 1)


# ---------------------------------------------------------------------------
# AC #5: /gasto con y sin fecha
# ---------------------------------------------------------------------------


def test_gasto_without_date_registers_with_today(db_session_factory):
    """/gasto sin fecha registra el gasto con la fecha de hoy por defecto."""
    resp = _send(db_session_factory, "/gasto 80000 | arriendo local")

    expenses = _expenses(db_session_factory)
    assert len(expenses) == 1
    assert expenses[0].total_amount == Decimal("80000.00")
    assert expenses[0].description == "arriendo local"
    assert expenses[0].expense_date == today_bogota()
    assert expenses[0].recorded_via == expense_service.RECORDED_VIA_TELEGRAM
    assert "✅ Gasto registrado: $80,000 COP — arriendo local" in resp


def test_gasto_without_description_and_without_date(db_session_factory):
    """/gasto <total> registra el gasto sin descripción y con fecha de hoy."""
    resp = _send(db_session_factory, "/gasto 50000")

    expenses = _expenses(db_session_factory)
    assert len(expenses) == 1
    assert expenses[0].total_amount == Decimal("50000.00")
    assert expenses[0].description is None
    assert expenses[0].expense_date == today_bogota()
    assert "✅ Gasto registrado: $50,000 COP" in resp
    assert "—" not in resp


def test_gasto_with_previous_month_date(db_session_factory):
    """/gasto con fecha del mes anterior la toma y la incluye en la confirmación."""
    prev_d = _prev_month_date()
    date_str_input = f"{prev_d.day:02d}/{prev_d.month:02d}"
    expected_str = format_limit_date(prev_d)

    resp = _send(db_session_factory, f"/gasto 120000 | papelería | {date_str_input}")

    expenses = _expenses(db_session_factory)
    assert len(expenses) == 1
    assert expenses[0].total_amount == Decimal("120000.00")
    assert expenses[0].description == "papelería"
    assert expenses[0].expense_date == prev_d
    assert f"✅ Gasto registrado: $120,000 COP — papelería ({expected_str})" in resp


def test_gasto_with_date_no_description(db_session_factory):
    """/gasto <total>, <fecha> registra sin descripción y confirma con fecha."""
    prev_d = _prev_month_date()
    date_str_input = f"{prev_d.day:02d}/{prev_d.month:02d}"
    expected_str = format_limit_date(prev_d)

    resp = _send(db_session_factory, f"/gasto 95000, {date_str_input}")

    expenses = _expenses(db_session_factory)
    assert len(expenses) == 1
    assert expenses[0].total_amount == Decimal("95000.00")
    assert expenses[0].description is None
    assert expenses[0].expense_date == prev_d
    assert f"✅ Gasto registrado: $95,000 COP ({expected_str})" in resp


def test_gasto_with_iso_and_full_date_formats(db_session_factory):
    """/gasto soporta formato AAAA-MM-DD y DD/MM/AAAA."""
    prev_d = _prev_month_date()
    iso_input = prev_d.isoformat()
    dma_input = f"{prev_d.day:02d}/{prev_d.month:02d}/{prev_d.year}"
    expected_str = format_limit_date(prev_d)

    # Formato ISO con punto y coma
    resp1 = _send(db_session_factory, f"/gasto 60000; servicios; {iso_input}")
    assert f"✅ Gasto registrado: $60,000 COP — servicios ({expected_str})" in resp1

    # Formato DD/MM/AAAA con pipe
    resp2 = _send(db_session_factory, f"/gasto 70000 | mantenimiento | {dma_input}")
    assert f"✅ Gasto registrado: $70,000 COP — mantenimiento ({expected_str})" in resp2


# ---------------------------------------------------------------------------
# AC #3 & #2: Fechas fuera de rango o inválidas
# ---------------------------------------------------------------------------


def test_gasto_date_out_of_range(db_session_factory):
    """AC #3: Fecha fuera de rango responde con '❌ ' + mensaje del servicio DATE_OUT_OF_RANGE."""
    out_d = _out_of_range_date()
    resp = _send(db_session_factory, f"/gasto 80000 | compra vieja | {out_d.isoformat()}")

    assert resp.startswith("❌ Solo puedes registrar con fecha de este mes o del mes anterior")
    assert _expenses(db_session_factory) == []

    # Fecha futura
    future_d = today_bogota() + timedelta(days=5)
    resp_future = _send(db_session_factory, f"/gasto 80000 | futuro | {future_d.isoformat()}")
    assert resp_future.startswith("❌ Solo puedes registrar con fecha de este mes o del mes anterior")
    assert _expenses(db_session_factory) == []


def test_gasto_invalid_calendar_date_shows_format_help(db_session_factory):
    """AC #2: Fecha inexistente (ej. 31/02) muestra el mensaje de ayuda de formato."""
    resp = _send(db_session_factory, "/gasto 80000 | arriendo | 31/02")

    assert "Formato: `/gasto <total> | <descripción opcional> | <fecha opcional>`" in resp
    assert _expenses(db_session_factory) == []


# ---------------------------------------------------------------------------
# Repercusiones de perfil y suscripción en /gasto
# ---------------------------------------------------------------------------


def test_gasto_rejections_dian_and_blocked(db_session_factory):
    """AC #5: Negocio DIAN y clientes bloqueados no pueden registrar gastos."""
    # Negocio DIAN
    resp_dian = _send(db_session_factory, "/gasto 50000", chat_id=DIAN_CHAT)
    assert "ℹ️ Tu perfil no registra gastos" in resp_dian

    # Suscripción BLOQUEADA
    resp_bloq = _send(db_session_factory, "/gasto 50000", chat_id=BLOCKED_CHAT)
    assert "Servicio Suspendido" in resp_bloq

    # Chat no vinculado
    resp_unlinked = _send(db_session_factory, "/gasto 50000", chat_id=UNLINKED_CHAT)
    assert "Cuenta no vinculada" in resp_unlinked

    assert _expenses(db_session_factory) == []


def test_gasto_invalid_amounts_show_help(db_session_factory):
    """Total inválido o no positivo muestra ayuda de formato de /gasto."""
    # Sin argumentos
    resp_empty = _send(db_session_factory, "/gasto")
    assert "Formato: `/gasto <total> | <descripción opcional> | <fecha opcional>`" in resp_empty
    assert _expenses(db_session_factory) == []

    # Texto en lugar de número
    resp_abc = _send(db_session_factory, "/gasto abc")
    assert "Formato: `/gasto <total> | <descripción opcional> | <fecha opcional>`" in resp_abc
    assert _expenses(db_session_factory) == []

    # Monto no positivo
    resp_zero = _send(db_session_factory, "/gasto 0")
    assert "El total debe ser mayor a cero." in resp_zero
    assert "/gasto <total>" in resp_zero

    resp_neg = _send(db_session_factory, "/gasto -5000")
    assert "El total debe ser mayor a cero." in resp_neg
    assert "/gasto <total>" in resp_neg


# ---------------------------------------------------------------------------
# AC #6 & #7: /mis_gastos y /anular_gasto
# ---------------------------------------------------------------------------


def test_mis_gastos_and_anular_gasto_flow(db_session_factory):
    """AC #6 & #7: /mis_gastos lista gastos numerados y /anular_gasto N anula según número estable."""
    # Registrar dos gastos
    prev_d = _prev_month_date()
    _send(db_session_factory, "/gasto 30000 | insumos")
    _send(db_session_factory, f"/gasto 40000 | transporte | {prev_d.day:02d}/{prev_d.month:02d}")

    # Listar con /mis_gastos
    resp_list = _send(db_session_factory, "/mis_gastos")
    assert "💸 *Tus últimos gastos*" in resp_list
    assert "transporte" in resp_list
    assert "insumos" in resp_list
    assert "Para anular uno: `/anular_gasto 2`" in resp_list

    # Anular el gasto 2 (transporte)
    resp_void = _send(db_session_factory, "/anular_gasto 2")
    assert "🗑️ Gasto anulado: $40,000 COP — transporte" in resp_void
    assert prev_d.strftime("%d/%m/%Y") in resp_void

    # /mis_gastos ahora solo muestra el gasto 1
    resp_list2 = _send(db_session_factory, "/mis_gastos")
    assert "insumos" in resp_list2
    assert "transporte" not in resp_list2
    assert "Para anular uno: `/anular_gasto 1`" in resp_list2


def test_mis_gastos_empty_and_dian_rejection(db_session_factory):
    """/mis_gastos vacío informa adecuadamente, y perfil DIAN es rechazado."""
    resp_empty = _send(db_session_factory, "/mis_gastos")
    assert "ℹ️ Aún no tienes gastos registrados." in resp_empty

    # Perfil DIAN
    resp_dian = _send(db_session_factory, "/mis_gastos", chat_id=DIAN_CHAT)
    assert "ℹ️ Tu perfil no registra gastos." in resp_dian

    resp_anular_dian = _send(db_session_factory, "/anular_gasto 1", chat_id=DIAN_CHAT)
    assert "ℹ️ Tu perfil no registra gastos." in resp_anular_dian


def test_anular_gasto_validation_and_errors(db_session_factory):
    """/anular_gasto valida formato de número y número fuera de rango o ya anulado."""
    _send(db_session_factory, "/gasto 50000 | servicio de agua")

    # Sin número
    resp_no_num = _send(db_session_factory, "/anular_gasto")
    assert "❌ Indica el número del gasto a anular" in resp_no_num

    # Número que no existe
    resp_out = _send(db_session_factory, "/anular_gasto 99")
    assert "❌ Ese número no está en tu lista actual" in resp_out

    # Anulación exitosa
    resp_void = _send(db_session_factory, "/anular_gasto 1")
    assert "🗑️ Gasto anulado:" in resp_void

    # Intentar anular de nuevo el mismo número
    resp_again = _send(db_session_factory, "/anular_gasto 1")
    assert "❌ Ese número no está en tu lista actual (o el gasto ya está anulado)." in resp_again


# ---------------------------------------------------------------------------
# AC #1, #3, #4, #8: /venta con fecha del mes anterior
# ---------------------------------------------------------------------------


def test_venta_with_previous_month_date(db_session_factory):
    """AC #1, #4, #8: /venta con fecha del mes anterior guarda con esa sale_date y confirma con fecha."""
    prev_d = _prev_month_date()
    date_str_input = f"{prev_d.day:02d}/{prev_d.month:02d}"
    expected_str = format_limit_date(prev_d)

    resp = _send(db_session_factory, f"/venta 150000 | tortas | {date_str_input}")

    sales = _sales(db_session_factory)
    assert len(sales) == 1
    assert sales[0].total_amount == Decimal("150000.00")
    assert sales[0].description == "tortas"
    assert sales[0].sale_date == prev_d
    assert f"✅ Venta registrada: $150,000 COP — tortas ({expected_str})" in resp

    # /mis_ventas muestra la fecha del mes anterior
    resp_mis_ventas = _send(db_session_factory, "/mis_ventas")
    assert prev_d.strftime("%d/%m/%Y") in resp_mis_ventas
    assert "$150,000 COP — tortas" in resp_mis_ventas


def test_venta_with_date_no_description(db_session_factory):
    """/venta 150000, 28/10 (sin descripción) guarda con fecha y confirma con fecha."""
    prev_d = _prev_month_date()
    date_str_input = f"{prev_d.day:02d}/{prev_d.month:02d}"
    expected_str = format_limit_date(prev_d)

    resp = _send(db_session_factory, f"/venta 150000, {date_str_input}")

    sales = _sales(db_session_factory)
    assert len(sales) == 1
    assert sales[0].total_amount == Decimal("150000.00")
    assert sales[0].description is None
    assert sales[0].sale_date == prev_d
    assert f"✅ Venta registrada: $150,000 COP ({expected_str})" in resp


def test_venta_date_out_of_range_and_invalid(db_session_factory):
    """AC #3: Fecha fuera de rango en /venta rechaza con mensaje del servicio."""
    out_d = _out_of_range_date()
    resp = _send(db_session_factory, f"/venta 150000 | tortas | {out_d.isoformat()}")

    assert resp.startswith("❌ Solo puedes registrar con fecha de este mes o del mes anterior")
    assert _sales(db_session_factory) == []

    # Fecha 31/02
    resp_feb = _send(db_session_factory, "/venta 150000 | tortas | 31/02")
    assert "Formato: `/venta <total> | <descripción opcional> | <fecha opcional>`" in resp_feb
    assert _sales(db_session_factory) == []


# ---------------------------------------------------------------------------
# AC #9: /resumen en perfil Ventas y Mixto
# ---------------------------------------------------------------------------


def test_resumen_ventas_profile_shows_gastos_and_note(db_session_factory):
    """AC #9: /resumen Ventas agrega 'Gastos: $X (N)' debajo de Egresos y nota de utilidad."""
    db = db_session_factory()
    try:
        today = date.today()
        # Venta de $1,000,000
        sale = Sale(
            business_id="biz-pedro-manual",
            total_amount=Decimal("1000000.00"),
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            sale_date=today,
            created_at=datetime.utcnow(),
        )
        # Egreso de $200,000 (factura recibida)
        inv = Invoice(
            id="inv-recibida-resumen",
            business_id="biz-pedro-manual",
            cufe="cufe-egreso-resumen",
            document_type="Factura electrónica de venta",
            prefix="REC",
            folio="801",
            issue_date=datetime.utcnow(),
            issuer_nit="800111222",
            issuer_name="Proveedor Harina SAS",
            receiver_nit="900888777",
            receiver_name="Panadería Pedro Gómez SAS",
            iva=Decimal("19000.00"),
            total=Decimal("200000.00"),
            group_type="Recibido",
        )
        # Gasto de $80,000
        expense = Expense(
            business_id="biz-pedro-manual",
            total_amount=Decimal("80000.00"),
            description="bolsas de papel",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-pedro-manual",
            expense_date=today,
            created_at=datetime.utcnow(),
        )
        db.add_all([sale, inv, expense])
        db.commit()

        resp = ClientTelegramBot.handle_resumen(sender_chat_id=MANUAL_CHAT, db=db)
        assert resp["success"] is True

        msg = resp["message"]
        assert "💵 *Ingresos:* $1,000,000 COP" in msg
        assert "💸 *Egresos:* $200,000 COP" in msg
        # Línea de gastos debajo de egresos
        assert "💸 *Gastos:* $80,000 COP (1)" in msg
        # La utilidad sigue siendo 1,000,000 - 200,000 = 800,000 (el gasto NO resta de utilidad)
        assert "📈 *Utilidad estimada:* $800,000 COP" in msg
        # Nota explícita
        assert "Los gastos no se restan de la utilidad" in msg
    finally:
        db.close()


def test_resumen_mixed_profile_shows_gastos_without_utility(db_session_factory):
    """AC #9: /resumen Mixto muestra la línea de gastos y NO tiene utilidad ni nota de no restar."""
    db = db_session_factory()
    try:
        today = date.today()
        # Factura emitida
        inv_emit = Invoice(
            id="inv-emit-mixed",
            business_id="biz-carlos-mixed",
            cufe="cufe-emit-mixed",
            document_type="Factura electrónica de venta",
            prefix="FE",
            folio="101",
            issue_date=datetime.utcnow(),
            issuer_nit="901222333",
            issuer_name="Comercial Mixta SAS",
            receiver_nit="900111222",
            receiver_name="Cliente Mixto",
            iva=Decimal("19000.00"),
            total=Decimal("500000.00"),
            group_type="Emitido",
        )
        # Venta manual
        sale = Sale(
            business_id="biz-carlos-mixed",
            total_amount=Decimal("300000.00"),
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-carlos-mixed",
            sale_date=today,
            created_at=datetime.utcnow(),
        )
        # Factura recibida (egreso)
        inv_rec = Invoice(
            id="inv-rec-mixed",
            business_id="biz-carlos-mixed",
            cufe="cufe-rec-mixed",
            document_type="Factura electrónica de venta",
            prefix="REC",
            folio="201",
            issue_date=datetime.utcnow(),
            issuer_nit="800111222",
            issuer_name="Proveedor Mayorista SAS",
            receiver_nit="901222333",
            receiver_name="Comercial Mixta SAS",
            iva=Decimal("19000.00"),
            total=Decimal("150000.00"),
            group_type="Recibido",
        )
        # Gasto
        expense = Expense(
            business_id="biz-carlos-mixed",
            total_amount=Decimal("80000.00"),
            description="cinta de embalaje",
            recorded_via="TELEGRAM",
            recorded_by_user_id="usr-carlos-mixed",
            expense_date=today,
            created_at=datetime.utcnow(),
        )
        db.add_all([inv_emit, sale, inv_rec, expense])
        db.commit()

        resp = ClientTelegramBot.handle_resumen(sender_chat_id=MIXED_CHAT, db=db)
        assert resp["success"] is True

        msg = resp["message"]
        assert "🧾 *Facturado:* $500,000 COP" in msg
        assert "🛒 *Ventas:* $300,000 COP" in msg
        assert "💸 *Egresos:* $150,000 COP" in msg
        # Línea de gastos
        assert "💸 *Gastos:* $80,000 COP (1)" in msg
        # En Mixto NO hay utilidad ni nota de no restar de utilidad
        assert "Utilidad" not in msg
        assert "no se restan de la utilidad" not in msg
    finally:
        db.close()


# ---------------------------------------------------------------------------
# AC #10: Ayuda y Enrutado
# ---------------------------------------------------------------------------


def test_ayuda_sales_and_mixed_profile_lists_gastos_and_date_help(db_session_factory):
    """AC #10: /ayuda en perfiles con ventas lista /gasto, /mis_gastos, /anular_gasto y explica la fecha."""
    # Ventas
    help_ventas = _send(db_session_factory, "/ayuda", chat_id=MANUAL_CHAT)
    assert "*/gasto*" in help_ventas
    assert "*/mis_gastos*" in help_ventas
    assert "*/anular_gasto*" in help_ventas
    assert "Puedes poner una fecha de este mes o del mes anterior al final: `/venta 150000 | tortas | 28/10`" in help_ventas

    # Mixto
    help_mixed = _send(db_session_factory, "/ayuda", chat_id=MIXED_CHAT)
    assert "*/gasto*" in help_mixed
    assert "*/mis_gastos*" in help_mixed
    assert "*/anular_gasto*" in help_mixed
    assert "Puedes poner una fecha de este mes o del mes anterior al final: `/venta 150000 | tortas | 28/10`" in help_mixed

    # DIAN no lista comandos de gastos ni ventas
    help_dian = _send(db_session_factory, "/ayuda", chat_id=DIAN_CHAT)
    assert "/gasto" not in help_dian
    assert "/mis_gastos" not in help_dian
    assert "/anular_gasto" not in help_dian
    assert "/venta" not in help_dian


def test_router_gasto_regex_and_bot_mentions(db_session_factory):
    """Dev Notes: Regex /gasto(?:@\w+)?(?=\s|$) no se confunde con /gastos ni otros comandos."""
    # /gasto@KontableBot con sufijo
    resp = _send(db_session_factory, "/gasto@KontableBot 25000 | café oficina")
    assert "Gasto registrado: $25,000 COP" in resp

    # /mis_gastos@KontableBot con sufijo
    resp_list = _send(db_session_factory, "/mis_gastos@KontableBot")
    assert "Tus últimos gastos" in resp_list
    assert "café oficina" in resp_list

    # /anular_gasto@KontableBot con sufijo
    resp_void = _send(db_session_factory, "/anular_gasto@KontableBot 1")
    assert "Gasto anulado:" in resp_void

    # /gastos no debe confundirse con /gasto (cae en no reconocido)
    resp_gastos = _send(db_session_factory, "/gastos")
    assert "🤖 No reconozco ese comando." in resp_gastos

    resp_gastos_arg = _send(db_session_factory, "/gastos 100")
    assert "🤖 No reconozco ese comando." in resp_gastos_arg
