"""Pruebas unitarias para core/admin_service.py (Story 8.1 - AC #1 y #4)."""

from datetime import date, datetime, timedelta
from decimal import Decimal
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.core.admin_service import (
    AdminServiceError,
    NewClientData,
    calculate_dian_dv,
    confirm_payment,
    create_client,
    enqueue_extraction,
    list_clients,
    new_activation_link,
    normalize_nit,
    resolve_months_range,
    set_income_source,
    set_tax_profile,
    release_telegram,
)
from dian_automation.db.models import (
    Base,
    Business,
    DIANExtractionJob,
    PaymentRecord,
    Subscription,
    TelegramLinkToken,
    User,
)


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria para pruebas del servicio de administración."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = TestingSessionLocal()
    admin_user = User(
        id="usr-admin-katerinn",
        email="katerinn.admin@kontable.co",
        full_name="Katerinn Admin",
        role="ADMIN",
        telegram_chat_id=555444333,
        is_active=True,
    )
    db.add(admin_user)
    db.commit()
    db.close()

    return TestingSessionLocal


def test_calculate_dian_dv():
    """Valida cálculo de DV módulo 11."""
    assert calculate_dian_dv("901008579") == "7"
    assert calculate_dian_dv("800197268") == "4"
    assert calculate_dian_dv("") == "0"


def test_normalize_nit():
    """Valida normalización de NIT y extracción de DV."""
    assert normalize_nit("901008579-7") == "901008579"
    assert normalize_nit(" 901.008.579 ") == "901008579"
    assert normalize_nit("123") is None
    assert normalize_nit(None) is None


def test_resolve_months_range():
    """Valida cálculo de rangos de meses completos sin el mes en curso."""
    rango = resolve_months_range(6, reference_date=date(2026, 9, 15))
    assert rango == "2026-03-01 - 2026-08-31"

    rango_1 = resolve_months_range(1, reference_date=date(2026, 9, 15))
    assert rango_1 == "2026-08-01 - 2026-08-31"


def test_create_client_persona_dian_and_manual_sales(db_session_factory):
    """Crea clientes personas naturales para DIAN y para ventas manuales."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()

        # Persona DIAN
        data_dian = NewClientData(
            tipo_cliente="PERSONA",
            full_name="Laura Gomez",
            phone="3001112233",
            nit="1000000001",
            plan="TRIMESTRAL",
        )
        created_dian = create_client(db, admin, data_dian)
        assert created_dian.user.full_name == "Laura Gomez"
        assert created_dian.business.income_source == "DIAN"
        assert created_dian.business.taxpayer_type == "PERSONA_NATURAL"
        assert created_dian.subscription.plan == "TRIMESTRAL"
        assert created_dian.subscription.status == "ACTIVO"
        assert created_dian.final_price == 142500.0
        assert "t.me" in created_dian.deep_link_url

        # Persona Ventas Manuales
        data_manual = NewClientData(
            tipo_cliente="PERSONA",
            full_name="Carlos Ruiz",
            phone="3002223344",
            nit="1000000002",
            plan="SEMESTRAL",
            income_source="VENTAS_MANUALES",
        )
        created_manual = create_client(db, admin, data_manual)
        assert created_manual.business.income_source == "MANUAL_SALES"
        assert created_manual.final_price == 276000.0
    finally:
        db.close()


def test_create_client_empresa(db_session_factory):
    """Crea cliente empresa jurídica con razón social y representante."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()
        data = NewClientData(
            tipo_cliente="EMPRESA",
            full_name="Andrea Contacto",
            phone="3003334455",
            business_name="Ferreteria El Roble SAS",
            nit="901008579",
            legal_rep_doc="10000002",
            plan="ANUAL",
        )
        created = create_client(db, admin, data)
        assert created.tipo_cliente == "EMPRESA"
        assert created.business.legal_name == "Ferreteria El Roble SAS"
        assert created.business.legal_rep_doc == "10000002"
        assert created.business.taxpayer_type == "PERSONA_JURIDICA"
        assert created.subscription.plan == "ANUAL"
        assert created.final_price == 540000.0
    finally:
        db.close()


def test_create_client_recreation_type_handling(db_session_factory):
    """Recrear un cliente sin tipo conserva el tipo existente; con tipo explícito lo actualiza."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()
        data1 = NewClientData(
            tipo_cliente="PERSONA",
            full_name="Maria Lopez",
            phone="3201112233",
            nit="1000000005",
            plan="TRIMESTRAL",
            income_source="VENTAS_MANUALES",
        )
        created1 = create_client(db, admin, data1)
        assert created1.business.income_source == "MANUAL_SALES"

        # Recrear sin income_source -> conserva MANUAL_SALES
        data2 = NewClientData(
            tipo_cliente="PERSONA",
            full_name="Maria Lopez",
            phone="3201112233",
            nit="1000000005",
            plan="SEMESTRAL",
        )
        created2 = create_client(db, admin, data2)
        assert created2.business.income_source == "MANUAL_SALES"

        # Recrear con FACTURADOR -> cambia a DIAN
        data3 = NewClientData(
            tipo_cliente="PERSONA",
            full_name="Maria Lopez",
            phone="3201112233",
            nit="1000000005",
            plan="SEMESTRAL",
            income_source="FACTURADOR",
        )
        created3 = create_client(db, admin, data3)
        assert created3.business.income_source == "DIAN"
    finally:
        db.close()


def test_create_client_validation_errors(db_session_factory):
    """Valida rechazo tipado de datos incompletos o erróneos con AdminServiceError."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()

        # Plan inválido
        with pytest.raises(AdminServiceError) as exc_plan:
            create_client(db, admin, NewClientData(
                tipo_cliente="PERSONA",
                full_name="A",
                phone="123",
                nit="1000000001",
                plan="MENSUAL_INEXISTENTE",
            ))
        assert exc_plan.value.code == "INVALID_PLAN"

        # Tipo cliente inválido
        with pytest.raises(AdminServiceError) as exc_tipo:
            create_client(db, admin, NewClientData(
                tipo_cliente="OTRO",
                full_name="A",
                phone="123",
                nit="1000000001",
                plan="TRIMESTRAL",
            ))
        assert exc_tipo.value.code == "INVALID_CLIENT_TYPE"

        # Empresa sin business_name
        with pytest.raises(AdminServiceError) as exc_biz:
            create_client(db, admin, NewClientData(
                tipo_cliente="EMPRESA",
                full_name="A",
                phone="123",
                nit="901008579",
                plan="TRIMESTRAL",
                business_name="",
                legal_rep_doc="10000002",
            ))
        assert exc_biz.value.code == "MISSING_BUSINESS_NAME"

        # Empresa sin representante
        with pytest.raises(AdminServiceError) as exc_rep:
            create_client(db, admin, NewClientData(
                tipo_cliente="EMPRESA",
                full_name="A",
                phone="123",
                nit="901008579",
                plan="TRIMESTRAL",
                business_name="Empresa SAS",
                legal_rep_doc="",
            ))
        assert exc_rep.value.code == "MISSING_LEGAL_REP"

        # NIT corto
        with pytest.raises(AdminServiceError) as exc_nit:
            create_client(db, admin, NewClientData(
                tipo_cliente="PERSONA",
                full_name="A",
                phone="123",
                nit="123",
                plan="TRIMESTRAL",
            ))
        assert exc_nit.value.code == "INVALID_NIT"
    finally:
        db.close()


def test_confirm_payment_reactivates_blocked_and_calls_notifier(db_session_factory):
    """Confirma un pago, reactiva la suscripción en mora/bloqueada y ejecuta el notificador."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()

        # Crear cliente
        client_data = NewClientData(
            tipo_cliente="PERSONA",
            full_name="Cliente Bloqueado",
            phone="3119998877",
            nit="1000000010",
            plan="TRIMESTRAL",
        )
        created = create_client(db, admin, client_data)
        user = created.user
        user.telegram_chat_id = 999888777
        user.is_telegram_linked = True

        sub = created.subscription
        sub.status = "BLOQUEADO"
        sub.cutoff_date = date.today() - timedelta(days=10)
        db.commit()

        notifications = []

        def fake_notifier(chat_id: int, message: str) -> bool:
            notifications.append((chat_id, message))
            return True

        result = confirm_payment(
            db=db,
            admin=admin,
            nit="1000000010",
            amount=Decimal("142500"),
            reference="REF-TEST-999",
            notifier=fake_notifier,
        )

        assert result.subscription.status == "ACTIVO"
        assert result.client_notified is True
        assert len(notifications) == 1
        assert notifications[0][0] == 999888777
        assert "REF-TEST-999" in notifications[0][1]

        # Verificar registro de pago persistido
        payment = db.query(PaymentRecord).filter(PaymentRecord.subscription_id == sub.id).first()
        assert payment is not None
        assert payment.amount == Decimal("142500")
        assert payment.reference_code == "REF-TEST-999"
        assert payment.verified_by_admin_id == admin.id
    finally:
        db.close()


def test_confirm_payment_errors(db_session_factory):
    """Errores esperados al confirmar pagos (NIT inexistente, monto negativo, ref vacía)."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()

        # NIT no encontrado
        with pytest.raises(AdminServiceError) as exc_biz:
            confirm_payment(db, admin, nit="999999999", amount=Decimal("10000"), reference="REF-1")
        assert exc_biz.value.code == "BUSINESS_NOT_FOUND"

        # Monto negativo o cero
        with pytest.raises(AdminServiceError) as exc_amt:
            confirm_payment(db, admin, nit="999999999", amount=Decimal("0"), reference="REF-1")
        assert exc_amt.value.code == "INVALID_AMOUNT"

        # Referencia vacía
        with pytest.raises(AdminServiceError) as exc_ref:
            confirm_payment(db, admin, nit="999999999", amount=Decimal("10000"), reference="")
        assert exc_ref.value.code == "INVALID_REFERENCE"
    finally:
        db.close()


def test_set_income_source(db_session_factory):
    """Cambia el origen de ingresos entre DIAN y ventas manuales."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()
        created = create_client(db, admin, NewClientData(
            tipo_cliente="PERSONA",
            full_name="Test Source",
            phone="3004445566",
            nit="1000000020",
            plan="TRIMESTRAL",
        ))

        biz = set_income_source(db, admin, nit="1000000020", income_source="VENTAS_MANUALES")
        assert biz.income_source == "MANUAL_SALES"

        biz2 = set_income_source(db, admin, nit="1000000020", income_source="FACTURADOR")
        assert biz2.income_source == "DIAN"

        # Tipo inválido
        with pytest.raises(AdminServiceError) as exc_bad:
            set_income_source(db, admin, nit="1000000020", income_source="INVALIDO")
        assert exc_bad.value.code == "INVALID_INCOME_SOURCE"

        # NIT inexistente
        with pytest.raises(AdminServiceError) as exc_missing:
            set_income_source(db, admin, nit="999999999", income_source="FACTURADOR")
        assert exc_missing.value.code == "BUSINESS_NOT_FOUND"
    finally:
        db.close()


def test_set_tax_profile(db_session_factory):
    """Configura periodicidad de IVA y retención en la fuente para facturadores."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()
        created = create_client(db, admin, NewClientData(
            tipo_cliente="EMPRESA",
            full_name="Contacto Tax",
            phone="3005556677",
            business_name="Empresa Tax SAS",
            nit="901008580",
            legal_rep_doc="10000002",
            plan="TRIMESTRAL",
        ))

        biz = set_tax_profile(
            db, admin, nit="901008580", iva_periodicity="CUATRIMESTRAL", is_withholding_agent=True
        )
        assert biz.iva_periodicity == "CUATRIMESTRAL"
        assert biz.is_withholding_agent is True

        # IVA NINGUNO
        biz_none = set_tax_profile(
            db, admin, nit="901008580", iva_periodicity="NINGUNO", is_withholding_agent=False
        )
        assert biz_none.iva_periodicity is None
        assert biz_none.is_withholding_agent is False

        # Rechazo si el negocio es de ventas manuales
        set_income_source(db, admin, nit="901008580", income_source="VENTAS_MANUALES")
        with pytest.raises(AdminServiceError) as exc_not_app:
            set_tax_profile(db, admin, nit="901008580", iva_periodicity="BIMESTRAL", is_withholding_agent=True)
        assert exc_not_app.value.code == "NOT_APPLICABLE"
    finally:
        db.close()


def test_release_telegram(db_session_factory):
    """Desvincula chat de Telegram de un usuario cliente."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()
        client = User(
            id="usr-to-release",
            email="to.release@kontable.co",
            full_name="Usuario Para Liberar",
            role="CLIENT",
            telegram_chat_id=777666555,
            is_telegram_linked=True,
            is_active=True,
        )
        db.add(client)
        db.commit()

        # Liberación exitosa
        released = release_telegram(db, admin, chat_id=777666555)
        assert released.telegram_chat_id is None
        assert released.is_telegram_linked is False

        # Intentar liberarse a sí mismo
        with pytest.raises(AdminServiceError) as exc_self:
            release_telegram(db, admin, chat_id=admin.telegram_chat_id)
        assert exc_self.value.code == "CANNOT_FREE_SELF"

        # Chat ID no encontrado
        with pytest.raises(AdminServiceError) as exc_missing:
            release_telegram(db, admin, chat_id=999000111)
        assert exc_missing.value.code == "USER_NOT_FOUND"
    finally:
        db.close()


def test_enqueue_extraction(db_session_factory):
    """Encola trabajo de extracción DIAN para un negocio existente."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()
        created = create_client(db, admin, NewClientData(
            tipo_cliente="PERSONA",
            full_name="Extraccion Test",
            phone="3007778899",
            nit="1000000030",
            plan="TRIMESTRAL",
        ))

        job = enqueue_extraction(db, admin, nit="1000000030", period_spec="2026-05")
        assert job.status == "ENQUEUED"
        assert job.target_period == "2026-05"

        # Período por defecto
        job_def = enqueue_extraction(db, admin, nit="1000000030")
        assert job_def.status == "ENQUEUED"
        assert len(job_def.target_period) == 7  # YYYY-MM

        # Negocio inexistente
        with pytest.raises(AdminServiceError) as exc_biz:
            enqueue_extraction(db, admin, nit="999999999")
        assert exc_biz.value.code == "BUSINESS_NOT_FOUND"
    finally:
        db.close()


def test_list_clients(db_session_factory):
    """Lista usuarios con rol CLIENT ordenados descendentemente."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()
        create_client(db, admin, NewClientData(
            tipo_cliente="PERSONA",
            full_name="Cliente 1",
            phone="3001110001",
            nit="1000000041",
            plan="TRIMESTRAL",
        ))
        create_client(db, admin, NewClientData(
            tipo_cliente="PERSONA",
            full_name="Cliente 2",
            phone="3001110002",
            nit="1000000042",
            plan="TRIMESTRAL",
        ))

        clients = list_clients(db, admin, limit=5)
        assert len(clients) == 2
        names = [c.full_name for c in clients]
        assert "Cliente 1" in names
        assert "Cliente 2" in names
    finally:
        db.close()


def test_new_activation_link_invalidates_previous_unused(db_session_factory):
    """Generar un nuevo enlace invalida los anteriores tokens no usados."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()
        created = create_client(db, admin, NewClientData(
            tipo_cliente="PERSONA",
            full_name="Cliente Token",
            phone="3009990011",
            nit="1000000050",
            plan="TRIMESTRAL",
        ))

        user_id = created.user.id
        tokens_initial = db.query(TelegramLinkToken).filter(TelegramLinkToken.user_id == user_id).all()
        assert len(tokens_initial) == 1
        assert tokens_initial[0].is_used is False
        token1_id = tokens_initial[0].id

        # Generar nuevo enlace
        url_new = new_activation_link(db, admin, user_id, bot_username="TestBot")
        assert "https://t.me/TestBot?start=" in url_new

        tokens_after = db.query(TelegramLinkToken).filter(TelegramLinkToken.user_id == user_id).all()
        assert len(tokens_after) == 2

        old_token = db.query(TelegramLinkToken).filter(TelegramLinkToken.id == token1_id).first()
        assert old_token.is_used is True  # Se invalidó el anterior

        active_tokens = [t for t in tokens_after if not t.is_used]
        assert len(active_tokens) == 1

        # Usuario inexistente
        with pytest.raises(AdminServiceError) as exc_usr:
            new_activation_link(db, admin, "user-inexistente")
        assert exc_usr.value.code == "USER_NOT_FOUND"
    finally:
        db.close()
