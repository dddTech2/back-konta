"""Pruebas unitarias para Story 2.2: Bot de Telegram para Administradora Comercial (Katerinn)."""

import pytest
from datetime import date, datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import Base, User, Business, Subscription, TelegramLinkToken, DIANExtractionJob
from dian_automation.telegram.admin_bot import AdminTelegramBot


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria para pruebas de comandos administrativos."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Sembrar administradora comercial (Katerinn) y cliente existente
    db = TestingSessionLocal()
    admin_user = User(
        id="usr-admin-katerinn",
        email="katerinn.admin@kontable.co",
        full_name="Katerinn",
        role="ADMIN",
        telegram_chat_id=555444333,
        is_active=True,
    )
    unauthorized_client = User(
        id="usr-client-intruder",
        email="client@intruder.co",
        full_name="Intruder Client",
        role="CLIENT",
        telegram_chat_id=111222333,
        is_active=True,
    )
    db.add_all([admin_user, unauthorized_client])
    db.commit()
    db.close()

    return TestingSessionLocal


def test_calculate_dian_dv():
    """Valida el cálculo del dígito de verificación DIAN con algoritmo oficial módulo 11."""
    # 901008579 -> DV 7
    assert AdminTelegramBot.calculate_dian_dv("901008579") == "7"
    # NITs conocidos
    assert AdminTelegramBot.calculate_dian_dv("800197268") == "4"
    # Con caracteres no numéricos
    assert AdminTelegramBot.calculate_dian_dv("NIT: 901.008.579") == "7"


def test_admin_authorization_enforcement(db_session_factory):
    """Comandos administrativos son rechazados si el remitente no es ADMIN activo."""
    db = db_session_factory()
    try:
        # Intento desde chat de un CLIENT
        res_client = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=111222333,
            text="/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Ferretería El Roble SAS | 901008579 | 10000002 | TRIMESTRAL",
            db=db,
        )
        assert res_client["success"] is False
        assert res_client["reason"] == "UNAUTHORIZED"
        assert "Acceso denegado" in res_client["message"]

        # Intento desde chat id desconocido
        res_unknown = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=999999999,
            text="/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Ferretería El Roble SAS | 901008579 | 10000002 | TRIMESTRAL",
            db=db,
        )
        assert res_unknown["success"] is False
        assert res_unknown["reason"] == "UNAUTHORIZED"
    finally:
        db.close()


def test_crear_cliente_empresa_trimestral_success(db_session_factory):
    """Katerinn crea un cliente EMPRESA con plan TRIMESTRAL: exige NIT empresa + cédula representante."""
    db = db_session_factory()
    try:
        admin_chat_id = 555444333
        cmd = "/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Ferretería El Roble SAS | 901008579 | 10000002 | TRIMESTRAL"

        res = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text=cmd,
            db=db,
            bot_username="KontableTestBot",
        )

        assert res["success"] is True
        assert res["tipo_cliente"] == "EMPRESA"
        assert res["nit"] == "901008579-7"
        assert res["legal_rep_doc"] == "10000002"
        assert res["plan"] == "TRIMESTRAL"
        assert res["final_price"] == 142500.0
        assert "https://t.me/KontableTestBot?start=" in res["deep_link_url"]

        # Verificar User en DB (el contacto, no la empresa)
        user = db.query(User).filter(User.id == res["user_id"]).first()
        assert user is not None
        assert user.full_name == "Andrea Torres"
        assert user.phone == "3001234567"
        assert user.role == "CLIENT"
        assert user.is_telegram_linked is False

        # Verificar Business en DB: nombre real de la empresa, sin sufijos inventados
        biz = db.query(Business).filter(Business.id == res["business_id"]).first()
        assert biz is not None
        assert biz.legal_name == "Ferretería El Roble SAS"
        assert biz.commercial_name == "Ferretería El Roble SAS"
        assert biz.nit == "901008579"
        assert biz.dv == "7"
        assert biz.client_id == user.id
        assert biz.taxpayer_type == "PERSONA_JURIDICA"
        assert biz.legal_rep_doc == "10000002"

        # Verificar Subscription en DB
        sub = db.query(Subscription).filter(Subscription.client_id == user.id).first()
        assert sub is not None
        assert sub.plan == "TRIMESTRAL"
        assert float(sub.discount_rate) == 5.00
        assert float(sub.base_price) == 150000.0
        assert float(sub.final_price) == 142500.0
        assert sub.cutoff_date == date.today() + timedelta(days=90)
        assert sub.grace_period_end == sub.cutoff_date + timedelta(days=3)
        assert sub.status == "ACTIVO"

        # Verificar Token de Deep Linking en DB
        tokens = db.query(TelegramLinkToken).filter(TelegramLinkToken.user_id == user.id).all()
        assert len(tokens) == 1
        assert tokens[0].is_used is False
        assert tokens[0].expires_at >= datetime.utcnow() + timedelta(hours=71)

        # Mensaje de respuesta listo para reenviar
        assert "Cliente creado con éxito" in res["message"]
        assert "Andrea Torres" in res["message"]
        assert "901008579-7" in res["message"]
        assert "$142,500" in res["message"]
        assert res["deep_link_url"] in res["message"]
    finally:
        db.close()


def test_crear_cliente_persona_natural_success(db_session_factory):
    """Katerinn crea un cliente PERSONA: solo exige la cédula, sin representante legal."""
    db = db_session_factory()
    try:
        admin_chat_id = 555444333
        cmd = "/crear_cliente PERSONA | Laura Jiménez | 3005556677 | 1000000001 | TRIMESTRAL"

        res = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text=cmd,
            db=db,
            bot_username="KontableTestBot",
        )

        assert res["success"] is True
        assert res["tipo_cliente"] == "PERSONA"
        assert res["legal_rep_doc"] is None

        biz = db.query(Business).filter(Business.id == res["business_id"]).first()
        assert biz is not None
        assert biz.nit == "1000000001"
        assert biz.taxpayer_type == "PERSONA_NATURAL"
        assert biz.legal_rep_doc is None
    finally:
        db.close()


def test_crear_cliente_semestral_and_anual_pricing(db_session_factory):
    """Verifica el cálculo de precios para planes SEMESTRAL (-8%) y ANUAL (-10%)."""
    db = db_session_factory()
    try:
        admin_chat_id = 555444333

        # Plan Semestral (Empresa)
        res_sem = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente EMPRESA | Carlos Gómez | 3112223344 | Comercializadora Gómez SAS | 900555666 | 10000002 | SEMESTRAL",
            db=db,
        )
        assert res_sem["success"] is True
        assert res_sem["final_price"] == 276000.0  # 300k - 8% (24k)

        sub_sem = db.query(Subscription).filter(Subscription.client_id == res_sem["user_id"]).first()
        assert float(sub_sem.discount_rate) == 8.00
        assert sub_sem.cutoff_date == date.today() + timedelta(days=180)

        # Plan Anual (Persona Natural)
        res_anu = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente PERSONA | Maria Lopez | 3209998877 | 901222333 | ANUAL",
            db=db,
        )
        assert res_anu["success"] is True
        assert res_anu["final_price"] == 540000.0  # 600k - 10% (60k)

        sub_anu = db.query(Subscription).filter(Subscription.client_id == res_anu["user_id"]).first()
        assert float(sub_anu.discount_rate) == 10.00
        assert sub_anu.cutoff_date == date.today() + timedelta(days=365)
    finally:
        db.close()


def test_crear_cliente_syntax_validation(db_session_factory):
    """Valida rechazo de sintaxis incorrecta, tipo inválido o planes no soportados."""
    db = db_session_factory()
    try:
        admin_chat_id = 555444333

        # Faltan campos
        res_short = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente PERSONA | Andrea Torres | 3001234567",
            db=db,
        )
        assert res_short["success"] is False
        assert res_short["reason"] == "INVALID_SYNTAX"
        assert "Faltan datos" in res_short["message"]

        # Tipo no reconocido
        res_bad_tipo = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente NEGOCIO | Andrea Torres | 3001234567 | 901008579 | TRIMESTRAL",
            db=db,
        )
        assert res_bad_tipo["success"] is False
        assert res_bad_tipo["reason"] == "INVALID_SYNTAX"
        assert "no reconocido" in res_bad_tipo["message"]

        # Empresa sin cédula de representante (faltan campos)
        res_missing_rep = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente EMPRESA | Andrea Torres | 3001234567 | 901008579 | TRIMESTRAL",
            db=db,
        )
        assert res_missing_rep["success"] is False
        assert res_missing_rep["reason"] == "INVALID_SYNTAX"
        assert "Empresa" in res_missing_rep["message"]

        # Plan inválido
        res_bad_plan = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente PERSONA | Andrea Torres | 3001234567 | 901008579 | MENSUAL",
            db=db,
        )
        assert res_bad_plan["success"] is False
        assert res_bad_plan["reason"] == "INVALID_SYNTAX"
        assert "Plan 'MENSUAL' no reconocido" in res_bad_plan["message"]

        # NIT muy corto
        res_bad_nit = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente PERSONA | Andrea Torres | 3001234567 | 123 | TRIMESTRAL",
            db=db,
        )
        assert res_bad_nit["success"] is False
        assert "no es válido" in res_bad_nit["message"]

        # Cédula de representante muy corta (Empresa)
        res_bad_rep = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Torres SAS | 901008579 | 12 | TRIMESTRAL",
            db=db,
        )
        assert res_bad_rep["success"] is False
        assert "Representante Legal" in res_bad_rep["message"]

        # Empresa sin nombre de empresa (campo vacío entre celular y NIT)
        res_missing_biz_name = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente EMPRESA | Andrea Torres | 3001234567 |  | 901008579 | 10000002 | TRIMESTRAL",
            db=db,
        )
        assert res_missing_biz_name["success"] is False
        assert "estar vacío" in res_missing_biz_name["message"]
    finally:
        db.close()


def test_handle_admin_message_routing(db_session_factory):
    """Verifica el enrutamiento de comandos en handle_admin_message (/clientes, /ayuda)."""
    db = db_session_factory()
    try:
        admin_chat_id = 555444333

        # /ayuda
        help_msg = AdminTelegramBot.handle_admin_message(admin_chat_id, "/ayuda", db)
        assert "COMANDOS DE ADMINISTRACIÓN COMERCIAL" in help_msg
        assert "/crear_cliente" in help_msg

        # /clientes (inicialmente sin clientes)
        list_msg = AdminTelegramBot.handle_admin_message(admin_chat_id, "/clientes", db)
        assert "CLIENTES REGISTRADOS" in list_msg or "No hay clientes" in list_msg

        # Crear uno y volver a listar
        AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente EMPRESA | Pedro Pascal | 3001112233 | Pascal Import SAS | 901777888 | 10000002 | TRIMESTRAL",
            db=db,
        )
        list_msg_after = AdminTelegramBot.handle_admin_message(admin_chat_id, "/clientes", db)
        assert "Pedro Pascal" in list_msg_after
        assert "901777888" in list_msg_after
    finally:
        db.close()


def test_ejecutar_extraccion_enqueues_job(db_session_factory):
    """Katerinn encola una extracción DIAN real para un negocio existente por NIT."""
    db = db_session_factory()
    try:
        admin_chat_id = 555444333
        AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Ferretería El Roble SAS | 901008579 | 10000002 | TRIMESTRAL",
            db=db,
        )

        res = AdminTelegramBot.execute_ejecutar_extraccion(
            sender_chat_id=admin_chat_id,
            text="/ejecutar_extraccion 901008579 | 2026-06",
            db=db,
        )

        assert res["success"] is True
        assert res["period"] == "2026-06"
        assert "encolad" in res["message"].lower()
        assert "run_worker.py" in res["message"]

        job = db.query(DIANExtractionJob).filter(DIANExtractionJob.id == res["job_id"]).first()
        assert job is not None
        assert job.status == "ENQUEUED"
        assert job.target_period == "2026-06"

        # Admite el NIT con DV pegado y período por defecto (mes calendario anterior al de hoy)
        today = date.today()
        year, month = today.year, today.month - 1
        if month == 0:
            month, year = 12, year - 1
        expected_default_period = f"{year:04d}-{month:02d}"

        res_default = AdminTelegramBot.execute_ejecutar_extraccion(
            sender_chat_id=admin_chat_id,
            text="/ejecutar_extraccion 901008579-7",
            db=db,
        )
        assert res_default["success"] is True
        assert res_default["period"] == expected_default_period

        # Rechaza NIT desconocido
        res_missing = AdminTelegramBot.execute_ejecutar_extraccion(
            sender_chat_id=admin_chat_id,
            text="/ejecutar_extraccion 999999999",
            db=db,
        )
        assert res_missing["success"] is False
        assert res_missing["reason"] == "BUSINESS_NOT_FOUND"

        # Rechaza remitente no autorizado
        res_unauth = AdminTelegramBot.execute_ejecutar_extraccion(
            sender_chat_id=111222333,
            text="/ejecutar_extraccion 901008579",
            db=db,
        )
        assert res_unauth["success"] is False
        assert res_unauth["reason"] == "UNAUTHORIZED"
    finally:
        db.close()


def test_resolve_months_range_uses_only_complete_months():
    """El rango de N meses nunca incluye el mes en curso (no está completo)."""
    # Hoy 2026-09-15, 6 meses -> marzo 1 a agosto 31 (6 meses completos, sin septiembre)
    rango = AdminTelegramBot.resolve_months_range(6, reference_date=date(2026, 9, 15))
    assert rango == "2026-03-01 - 2026-08-31"

    # 1 mes -> solo el mes calendario anterior completo
    rango_1 = AdminTelegramBot.resolve_months_range(1, reference_date=date(2026, 9, 15))
    assert rango_1 == "2026-08-01 - 2026-08-31"

    # Cruce de año: enero -> el mes anterior completo es diciembre del año pasado
    rango_cruce = AdminTelegramBot.resolve_months_range(3, reference_date=date(2026, 1, 10))
    assert rango_cruce == "2025-10-01 - 2025-12-31"


def test_ejecutar_extraccion_con_rango_de_meses(db_session_factory):
    """/ejecutar_extraccion admite 'N meses' como un solo rango, en vez de un mes exacto."""
    db = db_session_factory()
    try:
        admin_chat_id = 555444333
        AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente PERSONA | Laura Jiménez | 3005556677 | 1000000001 | TRIMESTRAL",
            db=db,
        )

        res = AdminTelegramBot.execute_ejecutar_extraccion(
            sender_chat_id=admin_chat_id,
            text="/ejecutar_extraccion 1000000001 | 6 meses",
            db=db,
        )

        assert res["success"] is True
        assert " - " in res["period"]  # es un rango, no un mes exacto
        assert "Rango de meses" in res["message"]

        job = db.query(DIANExtractionJob).filter(DIANExtractionJob.id == res["job_id"]).first()
        assert job is not None
        assert job.target_period == res["period"]

        # Formato pegado ("6meses") también debe funcionar
        res_pegado = AdminTelegramBot.execute_ejecutar_extraccion(
            sender_chat_id=admin_chat_id,
            text="/ejecutar_extraccion 1000000001 | 6meses",
            db=db,
        )
        assert res_pegado["success"] is True
        assert res_pegado["period"] == res["period"]

        # Rango fuera de límites (0 o más de 24 meses) se rechaza
        res_bad_range = AdminTelegramBot.execute_ejecutar_extraccion(
            sender_chat_id=admin_chat_id,
            text="/ejecutar_extraccion 1000000001 | 30 meses",
            db=db,
        )
        assert res_bad_range["success"] is False
        assert res_bad_range["reason"] == "INVALID_SYNTAX"
    finally:
        db.close()
