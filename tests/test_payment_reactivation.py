"""Pruebas unitarias para Story 3.4: Registro de Pagos y Reactivación Automática Inmediata."""

import pytest
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import Base, User, Business, Subscription, PaymentRecord
from dian_automation.telegram.admin_bot import AdminTelegramBot
from dian_automation.telegram.client_bot import ClientTelegramBot
from dian_automation.subscriptions.lockout_service import SubscriptionLockoutService


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria con administrador y cliente en mora."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = TestingSessionLocal()
    # Administradora Comercial (Katerinn)
    admin = User(
        id="usr-admin-katerinn",
        email="katerinn@kontable.co",
        full_name="Katerinn Administradora",
        role="ADMIN",
        telegram_chat_id=11223344,
        is_active=True,
    )
    # Impostor no administrador
    impostor = User(
        id="usr-impostor",
        email="impostor@externo.co",
        full_name="Usuario No Admin",
        role="CLIENT",
        telegram_chat_id=99999999,
        is_active=True,
    )
    # Cliente Suspendido (Mauricio)
    client = User(
        id="usr-mauricio-vencido",
        email="mauricio@distribuidora.co",
        full_name="Mauricio Vencido",
        role="CLIENT",
        telegram_chat_id=777888999,
        is_telegram_linked=True,
        is_active=True,
    )
    biz = Business(
        id="biz-mauricio",
        client_id=client.id,
        legal_name="Distribuidora Vencida SAS",
        commercial_name="Vencida Distribuciones",
        nit="901008579",
        dv="7",
        is_active=True,
    )
    sub = Subscription(
        id="sub-mauricio",
        client_id=client.id,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date.today() - timedelta(days=100),
        cutoff_date=date.today() - timedelta(days=10),
        grace_period_end=date.today() - timedelta(days=7),
        status="BLOQUEADO",
    )

    db.add_all([admin, impostor, client, biz, sub])
    db.commit()
    db.close()

    return TestingSessionLocal


def test_confirmar_pago_unauthorized_rejected(db_session_factory):
    """Comando /confirmar_pago es rechazado si no proviene de un ADMIN activo."""
    db = db_session_factory()
    try:
        resp = AdminTelegramBot.execute_confirmar_pago(
            sender_chat_id=99999999,  # Impostor
            text="/confirmar_pago 901008579 | 142500 | TR-112233",
            db=db,
        )
        assert resp["success"] is False
        assert resp["reason"] == "UNAUTHORIZED"
        assert "Acceso denegado" in resp["message"]
    finally:
        db.close()


def test_confirmar_pago_invalid_syntax_and_amount(db_session_factory):
    """Valida formato incompleto y montos inválidos."""
    db = db_session_factory()
    try:
        # Faltan campos
        res_syntax = AdminTelegramBot.execute_confirmar_pago(
            sender_chat_id=11223344,
            text="/confirmar_pago 901008579 | 142500",
            db=db,
        )
        assert res_syntax["success"] is False
        assert res_syntax["reason"] == "INVALID_SYNTAX"

        # Monto no numérico o <= 0
        res_monto = AdminTelegramBot.execute_confirmar_pago(
            sender_chat_id=11223344,
            text="/confirmar_pago 901008579 | 0 | REF-00",
            db=db,
        )
        assert res_monto["success"] is False
        assert res_monto["reason"] == "INVALID_SYNTAX"
        assert "mayor a cero" in res_monto["message"]
    finally:
        db.close()


def test_confirmar_pago_business_not_found(db_session_factory):
    """Informa si el NIT ingresado no existe en la base de datos."""
    db = db_session_factory()
    try:
        res = AdminTelegramBot.execute_confirmar_pago(
            sender_chat_id=11223344,
            text="/confirmar_pago 800999999 | 142500 | REF-123",
            db=db,
        )
        assert res["success"] is False
        assert res["reason"] == "BUSINESS_NOT_FOUND"
        assert "No se encontró ningún negocio" in res["message"]
    finally:
        db.close()


def test_confirmar_pago_reactivates_blocked_subscription_and_lifts_lockout(db_session_factory):
    """Flujo completo de reactivación: pago registrado, suscripción ACTIVA y levantamiento dual."""
    db = db_session_factory()
    sent_notifications = []

    def mock_tg_sender(chat_id: int, msg: str) -> bool:
        sent_notifications.append((chat_id, msg))
        return True

    try:
        # 1. Comprobar que ANTES del pago el cliente está bloqueado en Web y Telegram
        web_check_before = SubscriptionLockoutService.verify_user_web_access("usr-mauricio-vencido", db=db)
        assert web_check_before["allowed"] is False
        assert web_check_before["status_code"] == 403

        tg_check_before = ClientTelegramBot.handle_client_message(
            sender_chat_id=777888999, text="/resumen", db=db
        )
        assert "Servicio Suspendido" in tg_check_before

        # 2. Katerinn registra el pago exitoso (con NIT con guión de DV)
        res_pago = AdminTelegramBot.execute_confirmar_pago(
            sender_chat_id=11223344,
            text="/confirmar_pago 901008579-7 | 142500 | NEQUI-887766",
            db=db,
            telegram_sender=mock_tg_sender,
        )
        assert res_pago["success"] is True
        assert res_pago["status"] == "ACTIVO"
        assert res_pago["client_notified"] is True

        # 3. Validar registros en Base de Datos
        payment = db.query(PaymentRecord).filter(PaymentRecord.id == res_pago["payment_id"]).first()
        assert payment is not None
        assert payment.amount == Decimal("142500.00")
        assert payment.reference_code == "NEQUI-887766"
        assert payment.verified_by_admin_id == "usr-admin-katerinn"
        assert payment.payment_method == "TRANSFERENCIA"

        sub = db.query(Subscription).filter(Subscription.id == "sub-mauricio").first()
        assert sub.status == "ACTIVO"
        assert sub.last_notified_at is None
        # Nueva fecha de corte debe ser hoy + 3 meses (Plan Trimestral)
        expected_cutoff = date.today() + timedelta(days=90)  # aproximadamente
        assert sub.cutoff_date > date.today()
        assert sub.grace_period_end == sub.cutoff_date + timedelta(days=3)

        # 4. Validar notificación automática despachada a Telegram del cliente
        assert len(sent_notifications) == 1
        client_chat_id, client_msg = sent_notifications[0]
        assert client_chat_id == 777888999
        assert "¡Pago Confirmado y Servicio Reactivado!" in client_msg
        assert "Mauricio Vencido" in client_msg
        assert "$142,500 COP" in client_msg
        assert "NEQUI-887766" in client_msg
        assert "100% ACTIVA" in client_msg

        # 5. Validar levantamiento de bloqueo en Canal 1: Web / API
        web_check_after = SubscriptionLockoutService.verify_user_web_access("usr-mauricio-vencido", db=db)
        assert web_check_after["allowed"] is True
        assert web_check_after["status_code"] == 200
        assert web_check_after["has_warning_banner"] is False

        # 6. Validar levantamiento de bloqueo en Canal 2: Bot de Telegram
        tg_check_after = ClientTelegramBot.handle_client_message(
            sender_chat_id=777888999, text="/ayuda", db=db
        )
        assert "¡Hola, bienvenido a Kontable Bot!" in tg_check_after
        assert "Servicio Suspendido" not in tg_check_after
    finally:
        db.close()


def test_admin_router_confirmar_pago(db_session_factory):
    """El enrutador handle_admin_message procesa /confirmar_pago y muestra guía en /ayuda."""
    db = db_session_factory()
    try:
        resp_ayuda = AdminTelegramBot.handle_admin_message(sender_chat_id=11223344, text="/ayuda", db=db)
        assert "/confirmar_pago" in resp_ayuda
        assert "Confirmar Pago" in resp_ayuda

        resp_pago = AdminTelegramBot.handle_admin_message(
            sender_chat_id=11223344,
            text="/confirmar_pago 901008579 | 142500 | TR-0011",
            db=db,
        )
        assert "Pago registrado y suscripción reactivada" in resp_pago
    finally:
        db.close()
