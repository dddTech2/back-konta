"""Pruebas unitarias para Story 3.2: Cron de Gestión de Periodo de Gracia de 72 Horas y Notificaciones Diarias."""

import pytest
from datetime import datetime, date, timedelta
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import Base, User, Business, Subscription
from dian_automation.subscriptions.grace_cron import SubscriptionGraceCron


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria con semillas de prueba."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = TestingSessionLocal()
    user = User(
        id="usr-client-andrea",
        email="andrea@branding.co",
        full_name="Andrea Torres",
        role="CLIENT",
        telegram_chat_id=777888999,
        is_telegram_linked=True,
        is_active=True,
    )
    biz = Business(
        id="biz-andrea",
        client_id=user.id,
        legal_name="Andrea Torres Diseño SAS",
        commercial_name="Torres Branding & Studio",
        nit="901008579",
        dv="7",
        is_active=True,
    )
    db.add_all([user, biz])
    db.commit()
    db.close()

    return TestingSessionLocal


def test_cron_transitions_active_to_en_mora_on_cutoff_day(db_session_factory):
    """En la fecha de corte, la suscripción transiciona a EN_MORA y recibe notificación Día 1 (72h)."""
    db = db_session_factory()
    sent_messages = []

    def mock_telegram_sender(chat_id: int, message: str) -> bool:
        sent_messages.append((chat_id, message))
        return True

    try:
        today = date(2026, 9, 15)
        sub = Subscription(
            id="sub-day-1",
            client_id="usr-client-andrea",
            plan="TRIMESTRAL",
            discount_rate=Decimal("5.00"),
            base_price=Decimal("150000.00"),
            final_price=Decimal("142500.00"),
            start_date=date(2026, 6, 15),
            cutoff_date=today,  # Hoy es el corte
            grace_period_end=today + timedelta(days=3),  # 72h
            status="ACTIVO",
        )
        db.add(sub)
        db.commit()

        summary = SubscriptionGraceCron.run_daily_grace_check(
            db=db, execution_date=today, telegram_sender=mock_telegram_sender
        )

        assert summary["processed_count"] == 1
        assert summary["transitioned_to_mora"] == 1
        assert summary["notifications_sent"] == 1

        db.refresh(sub)
        assert sub.status == "EN_MORA"
        assert sub.last_notified_at is not None

        # Verificar mensaje enviado
        assert len(sent_messages) == 1
        chat_id, text = sent_messages[0]
        assert chat_id == 777888999
        assert "Día 1 de 3 de Gracia" in text
        assert "periodo de gracia de 72 horas" in text
        assert "$142,500 COP" in text
    finally:
        db.close()


def test_cron_notifies_day_2_of_grace(db_session_factory):
    """En el día 2 de gracia (corte + 1 día), envía notificación alertando 2 días restantes."""
    db = db_session_factory()
    sent_messages = []

    def mock_sender(chat_id: int, message: str) -> bool:
        sent_messages.append((chat_id, message))
        return True

    try:
        today = date(2026, 9, 16)
        cutoff = date(2026, 9, 15)
        sub = Subscription(
            id="sub-day-2",
            client_id="usr-client-andrea",
            plan="SEMESTRAL",
            discount_rate=Decimal("8.00"),
            base_price=Decimal("300000.00"),
            final_price=Decimal("276000.00"),
            start_date=date(2026, 3, 15),
            cutoff_date=cutoff,
            grace_period_end=cutoff + timedelta(days=3),
            status="EN_MORA",  # Ya en mora
            last_notified_at=datetime(2026, 9, 15, 8, 0, 0),  # Notificado ayer
        )
        db.add(sub)
        db.commit()

        summary = SubscriptionGraceCron.run_daily_grace_check(
            db=db, execution_date=today, telegram_sender=mock_sender
        )

        assert summary["notifications_sent"] == 1
        assert len(sent_messages) == 1
        text = sent_messages[0][1]
        assert "Día 2 de 3 de Gracia" in text
        assert "restan *2 días* de gracia" in text
        assert "$276,000 COP" in text
    finally:
        db.close()


def test_cron_notifies_day_3_of_grace_last_day(db_session_factory):
    """En el día 3 de gracia (corte + 2 días), envía alerta urgente de último día antes de corte."""
    db = db_session_factory()
    sent_messages = []

    def mock_sender(chat_id: int, message: str) -> bool:
        sent_messages.append((chat_id, message))
        return True

    try:
        today = date(2026, 9, 17)
        cutoff = date(2026, 9, 15)
        sub = Subscription(
            id="sub-day-3",
            client_id="usr-client-andrea",
            plan="ANUAL",
            discount_rate=Decimal("10.00"),
            base_price=Decimal("600000.00"),
            final_price=Decimal("540000.00"),
            start_date=date(2025, 9, 15),
            cutoff_date=cutoff,
            grace_period_end=cutoff + timedelta(days=3),
            status="EN_MORA",
            last_notified_at=datetime(2026, 9, 16, 8, 0, 0),  # Notificado ayer
        )
        db.add(sub)
        db.commit()

        summary = SubscriptionGraceCron.run_daily_grace_check(
            db=db, execution_date=today, telegram_sender=mock_sender
        )

        assert summary["notifications_sent"] == 1
        assert len(sent_messages) == 1
        text = sent_messages[0][1]
        assert "ÚLTIMO DÍA DE GRACIA" in text
        assert "Suspensión Inminente" in text
        assert "$540,000 COP" in text
    finally:
        db.close()


def test_cron_idempotency_prevents_duplicate_notifications_same_day(db_session_factory):
    """Si el cron corre múltiples veces en el mismo día calendario, no reenvía el mensaje."""
    db = db_session_factory()
    sent_messages = []

    def mock_sender(chat_id: int, message: str) -> bool:
        sent_messages.append((chat_id, message))
        return True

    try:
        today = date(2026, 9, 15)
        sub = Subscription(
            id="sub-idem",
            client_id="usr-client-andrea",
            plan="TRIMESTRAL",
            discount_rate=Decimal("5.00"),
            base_price=Decimal("150000.00"),
            final_price=Decimal("142500.00"),
            start_date=date(2026, 6, 15),
            cutoff_date=today,
            grace_period_end=today + timedelta(days=3),
            status="EN_MORA",
            last_notified_at=datetime(2026, 9, 15, 1, 0, 0),  # Ya notificado hoy a la 1 AM
        )
        db.add(sub)
        db.commit()

        summary = SubscriptionGraceCron.run_daily_grace_check(
            db=db, execution_date=today, telegram_sender=mock_sender
        )

        assert summary["processed_count"] == 1
        assert summary["notifications_sent"] == 0  # No se volvió a enviar
        assert len(sent_messages) == 0
        assert summary["details"][0]["already_notified_today"] is True
    finally:
        db.close()


def test_cron_ignores_subscriptions_before_cutoff(db_session_factory):
    """Suscripciones con fecha de corte posterior al día actual no son procesadas ni alteradas."""
    db = db_session_factory()
    try:
        today = date(2026, 9, 15)
        sub = Subscription(
            id="sub-future",
            client_id="usr-client-andrea",
            plan="SEMESTRAL",
            discount_rate=Decimal("8.00"),
            base_price=Decimal("300000.00"),
            final_price=Decimal("276000.00"),
            start_date=date(2026, 8, 1),
            cutoff_date=date(2027, 2, 1),  # Corte futuro
            grace_period_end=date(2027, 2, 4),
            status="ACTIVO",
        )
        db.add(sub)
        db.commit()

        summary = SubscriptionGraceCron.run_daily_grace_check(db=db, execution_date=today)
        assert summary["processed_count"] == 0
        assert summary["transitioned_to_mora"] == 0

        db.refresh(sub)
        assert sub.status == "ACTIVO"
    finally:
        db.close()


def test_cron_handles_unlinked_telegram_client_gracefully(db_session_factory):
    """Si el cliente no tiene Telegram vinculado, transiciona a EN_MORA sin generar error."""
    db = db_session_factory()
    try:
        # Cliente sin telegram
        user_unlinked = User(
            id="usr-unlinked-client",
            email="sin.telegram@test.co",
            full_name="Cliente Sin Telegram",
            role="CLIENT",
            telegram_chat_id=None,
            is_telegram_linked=False,
            is_active=True,
        )
        sub = Subscription(
            id="sub-unlinked",
            client_id=user_unlinked.id,
            plan="TRIMESTRAL",
            discount_rate=Decimal("5.00"),
            base_price=Decimal("150000.00"),
            final_price=Decimal("142500.00"),
            start_date=date(2026, 6, 15),
            cutoff_date=date(2026, 9, 15),
            grace_period_end=date(2026, 9, 18),
            status="ACTIVO",
        )
        db.add_all([user_unlinked, sub])
        db.commit()

        summary = SubscriptionGraceCron.run_daily_grace_check(
            db=db, execution_date=date(2026, 9, 15)
        )
        assert summary["transitioned_to_mora"] == 1
        assert summary["notifications_sent"] == 0

        db.refresh(sub)
        assert sub.status == "EN_MORA"
    finally:
        db.close()


def test_web_banner_indicator_and_payload():
    """Valida los métodos de soporte para el banner informativo de la aplicación web."""
    sub_activo = Subscription(id="sub-1", status="ACTIVO", final_price=Decimal("142500.00"), plan="TRIMESTRAL")
    assert SubscriptionGraceCron.has_payment_warning_banner(sub_activo) is False
    assert SubscriptionGraceCron.get_payment_warning_banner_info(sub_activo) is None

    sub_mora = Subscription(
        id="sub-2",
        status="EN_MORA",
        cutoff_date=date(2026, 9, 15),
        grace_period_end=date(2026, 9, 18),
        final_price=Decimal("276000.00"),
        plan="SEMESTRAL",
    )
    assert SubscriptionGraceCron.has_payment_warning_banner(sub_mora) is True

    # Banner en día 1 de gracia (warning)
    banner_info_day1 = SubscriptionGraceCron.get_payment_warning_banner_info(
        sub_mora, check_date=date(2026, 9, 15)
    )
    assert banner_info_day1["show_banner"] is True
    assert banner_info_day1["severity"] == "warning"
    assert banner_info_day1["days_left_in_grace"] == 3
    assert banner_info_day1["amount_due"] == 276000.0

    # Banner en día 3 de gracia (danger)
    banner_info_day3 = SubscriptionGraceCron.get_payment_warning_banner_info(
        sub_mora, check_date=date(2026, 9, 17)
    )
    assert banner_info_day3["severity"] == "danger"
    assert banner_info_day3["days_left_in_grace"] == 1
