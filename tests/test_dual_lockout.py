"""Pruebas unitarias para Story 3.3: Bloqueo Dual de Acceso en Web y Telegram al Vencer la Gracia."""

import pytest
from datetime import date, timedelta, datetime
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import Base, User, Business, Subscription, MonthlyTaxSummary, Invoice
from dian_automation.subscriptions.grace_cron import SubscriptionGraceCron
from dian_automation.subscriptions.lockout_service import (
    SubscriptionLockoutService,
    SubscriptionBlockedError,
)
from dian_automation.telegram.client_bot import ClientTelegramBot


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria con semillas para pruebas de bloqueo dual."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = TestingSessionLocal()
    # 1. Cliente en mora cuya gracia ya venció
    user_vencido = User(
        id="usr-client-vencido",
        email="vencido@empresa.co",
        full_name="Mauricio Vencido",
        role="CLIENT",
        telegram_chat_id=888999111,
        is_telegram_linked=True,
        is_active=True,
    )
    biz_vencido = Business(
        id="biz-vencido",
        client_id=user_vencido.id,
        legal_name="Comercial Vencida SAS",
        commercial_name="Vencido Comercial",
        nit="901222333",
        dv="5",
        is_active=True,
    )
    # 2. Cliente al día
    user_activo = User(
        id="usr-client-activo",
        email="activo@empresa.co",
        full_name="Lucía Activa",
        role="CLIENT",
        telegram_chat_id=888999222,
        is_telegram_linked=True,
        is_active=True,
    )
    biz_activo = Business(
        id="biz-activo",
        client_id=user_activo.id,
        legal_name="Lucía Activa SAS",
        commercial_name="Activa Studio",
        nit="901444555",
        dv="9",
        is_active=True,
    )
    # 3. Usuario Administrador
    admin = User(
        id="usr-admin",
        email="katerinn@kontable.co",
        full_name="Katerinn Administradora",
        role="ADMIN",
        telegram_chat_id=111111111,
        is_active=True,
    )

    db.add_all([user_vencido, biz_vencido, user_activo, biz_activo, admin])
    db.commit()
    db.close()

    return TestingSessionLocal


def test_cron_transitions_to_bloqueado_when_grace_expires(db_session_factory):
    """Al expirar el 3er día de gracia (today > grace_period_end), el cron transiciona a BLOQUEADO."""
    db = db_session_factory()
    sent_messages = []

    def mock_sender(chat_id: int, msg: str) -> bool:
        sent_messages.append((chat_id, msg))
        return True

    try:
        today = date(2026, 9, 20)
        # Suscripción con gracia vencida el 18 de septiembre
        sub = Subscription(
            id="sub-expirada",
            client_id="usr-client-vencido",
            plan="TRIMESTRAL",
            discount_rate=Decimal("5.00"),
            base_price=Decimal("150000.00"),
            final_price=Decimal("142500.00"),
            start_date=date(2026, 6, 15),
            cutoff_date=date(2026, 9, 15),
            grace_period_end=date(2026, 9, 18),  # Venció hace 2 días
            status="EN_MORA",
        )
        db.add(sub)
        db.commit()

        # Ejecutar cron
        summary = SubscriptionGraceCron.run_daily_grace_check(
            db=db, execution_date=today, telegram_sender=mock_sender
        )

        assert summary["locked_out_count"] == 1
        db.refresh(sub)
        assert sub.status == "BLOQUEADO"

        # Mensaje de Telegram despachado
        assert len(sent_messages) == 1
        chat_id, text = sent_messages[0]
        assert chat_id == 888999111
        assert "Servicio Suspendido" in text
        assert "Comunícate con Katerinn" in text
    finally:
        db.close()


def test_web_access_rejected_with_403_for_blocked_user(db_session_factory):
    """Peticiones autenticadas en la Web/API de un usuario BLOQUEADO son rechazadas con 403 Forbidden."""
    db = db_session_factory()
    try:
        sub = Subscription(
            id="sub-bloq-web",
            client_id="usr-client-vencido",
            plan="SEMESTRAL",
            discount_rate=Decimal("8.00"),
            base_price=Decimal("300000.00"),
            final_price=Decimal("276000.00"),
            start_date=date(2026, 1, 1),
            cutoff_date=date(2026, 7, 1),
            grace_period_end=date(2026, 7, 4),
            status="BLOQUEADO",
        )
        db.add(sub)
        db.commit()

        # 1. Verificación directa de acceso Web
        check = SubscriptionLockoutService.verify_user_web_access("usr-client-vencido", db=db)
        assert check["allowed"] is False
        assert check["status_code"] == 403
        assert check["error_code"] == "SUBSCRIPTION_BLOCKED"
        assert check["redirect_url"] == "/servicio-suspendido"
        assert "Comunícate con Katerinn" in check["message"]

        # 2. Guardián de middleware / endpoint
        user_vencido = db.query(User).filter(User.id == "usr-client-vencido").first()
        with pytest.raises(SubscriptionBlockedError) as exc_info:
            SubscriptionLockoutService.enforce_web_access(user_vencido, db=db)

        err = exc_info.value
        assert err.status_code == 403
        assert err.redirect_url == "/servicio-suspendido"
        assert "Katerinn" in err.message
    finally:
        db.close()


def test_web_access_allowed_for_active_and_grace_period_users(db_session_factory):
    """Usuarios activos o en periodo de gracia no son bloqueados en la Web."""
    db = db_session_factory()
    try:
        today = date(2026, 9, 16)
        # Suscripción activa
        sub_activa = Subscription(
            id="sub-ok",
            client_id="usr-client-activo",
            plan="ANUAL",
            discount_rate=Decimal("10.00"),
            base_price=Decimal("600000.00"),
            final_price=Decimal("540000.00"),
            start_date=date(2026, 1, 1),
            cutoff_date=date(2027, 1, 1),
            grace_period_end=date(2027, 1, 4),
            status="ACTIVO",
        )
        db.add(sub_activa)
        db.commit()

        check_activo = SubscriptionLockoutService.verify_user_web_access(
            "usr-client-activo", db=db, reference_date=today
        )
        assert check_activo["allowed"] is True
        assert check_activo["status_code"] == 200
        assert check_activo["has_warning_banner"] is False

        # Suscripción en periodo de gracia (EN_MORA, pero dentro de los 3 días)
        sub_gracia = Subscription(
            id="sub-gracia",
            client_id="usr-client-vencido",
            plan="TRIMESTRAL",
            discount_rate=Decimal("5.00"),
            base_price=Decimal("150000.00"),
            final_price=Decimal("142500.00"),
            start_date=date(2026, 6, 15),
            cutoff_date=date(2026, 9, 15),
            grace_period_end=date(2026, 9, 18),  # Aún en gracia el día 16
            status="EN_MORA",
        )
        db.add(sub_gracia)
        db.commit()

        check_gracia = SubscriptionLockoutService.verify_user_web_access(
            "usr-client-vencido", db=db, reference_date=today
        )
        assert check_gracia["allowed"] is True
        assert check_gracia["status_code"] == 200
        assert check_gracia["has_warning_banner"] is True
        assert check_gracia["days_left_in_grace"] == 2
    finally:
        db.close()


def test_admin_exempt_from_web_lockout(db_session_factory):
    """Los administradores nunca son bloqueados por el guardián web."""
    db = db_session_factory()
    try:
        check_admin = SubscriptionLockoutService.verify_user_web_access("usr-admin", db=db)
        assert check_admin["allowed"] is True
        assert check_admin["status_code"] == 200
        assert check_admin["role"] == "ADMIN"
    finally:
        db.close()


def test_telegram_client_bot_blocks_all_commands_and_hides_financial_data(db_session_factory):
    """En Telegram, cualquier comando (/resumen, /facturas, /vencimientos) responde con el aviso de suspensión sin datos."""
    db = db_session_factory()
    try:
        # Poner al cliente en estado BLOQUEADO
        sub_bloq = Subscription(
            id="sub-bloq-tg",
            client_id="usr-client-vencido",
            plan="TRIMESTRAL",
            discount_rate=Decimal("5.00"),
            base_price=Decimal("150000.00"),
            final_price=Decimal("142500.00"),
            start_date=date(2026, 5, 1),
            cutoff_date=date(2026, 8, 1),
            grace_period_end=date(2026, 8, 4),
            status="BLOQUEADO",
        )
        # Añadir resumen financiero ficticio para verificar que NUNCA sea expuesto
        resumen_secreto = MonthlyTaxSummary(
            business_id="biz-vencido",
            period_year_month="2026-08",
            total_invoiced_net=Decimal("89000000.00"),
            iva_generado=Decimal("16910000.00"),
            iva_descontable=Decimal("5000000.00"),
            iva_balance=Decimal("11910000.00"),
            total_invoices_count=45,
        )
        db.add_all([sub_bloq, resumen_secreto])
        db.commit()

        chat_id = 888999111  # Chat de Mauricio Vencido

        comandos_probados = ["/resumen", "/facturas", "/vencimientos", "/ayuda", "hola bot"]
        for cmd in comandos_probados:
            resp = ClientTelegramBot.handle_client_message(sender_chat_id=chat_id, text=cmd, db=db)
            # Debe contener el texto mandatorio
            assert "Tu suscripción se encuentra suspendida temporalmente por pago pendiente. Comunícate con Katerinn para reactivar tus reportes." in resp
            # No debe contener cifras ni balances financieros
            assert "89,000,000" not in resp
            assert "11,910,000" not in resp
            assert "IVA Generado" not in resp
    finally:
        db.close()
