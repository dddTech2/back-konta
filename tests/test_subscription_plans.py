"""Pruebas unitarias para Story 3.1: Planes de Suscripción con Descuentos Porcentuales."""

import pytest
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import Base, User, Subscription
from dian_automation.subscriptions.service import (
    SubscriptionService,
    PlanQuote,
    DEFAULT_BASE_MONTHLY_PRICE,
    add_months_to_date,
)


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria con semillas de prueba."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = TestingSessionLocal()
    client = User(
        id="usr-test-sub-client",
        email="cliente.sub@test.co",
        full_name="Cliente Prueba Suscripciones",
        role="CLIENT",
        is_active=True,
    )
    db.add(client)
    db.commit()
    db.close()

    return TestingSessionLocal


def test_trimestral_plan_quote_discount_and_dates():
    """Plan TRIMESTRAL aplica 5% de descuento sobre 3 meses y 72h (3 días) de gracia."""
    start = date(2026, 9, 15)
    quote = SubscriptionService.calculate_quote("TRIMESTRAL", start_date=start)

    assert quote.plan == "TRIMESTRAL"
    assert quote.months == 3
    assert quote.base_monthly_price == Decimal("50000.00")
    assert quote.gross_total == Decimal("150000.00")
    assert quote.discount_rate == Decimal("5.00")
    assert quote.discount_amount == Decimal("7500.00")
    assert quote.final_price == Decimal("142500.00")  # $150,000 - 5%

    # Fechas
    assert quote.start_date == start
    assert quote.cutoff_date == date(2026, 12, 15)  # 3 meses exactos
    assert quote.grace_period_end == date(2026, 12, 18)  # +3 días (72h)


def test_semestral_plan_quote_discount_and_dates():
    """Plan SEMESTRAL aplica 8% de descuento sobre 6 meses y 72h de gracia."""
    start = date(2026, 9, 15)
    quote = SubscriptionService.calculate_quote("SEMESTRAL", start_date=start)

    assert quote.plan == "SEMESTRAL"
    assert quote.months == 6
    assert quote.gross_total == Decimal("300000.00")
    assert quote.discount_rate == Decimal("8.00")
    assert quote.discount_amount == Decimal("24000.00")
    assert quote.final_price == Decimal("276000.00")  # $300,000 - 8%

    assert quote.cutoff_date == date(2027, 3, 15)  # 6 meses exactos
    assert quote.grace_period_end == date(2027, 3, 18)  # +3 días (72h)


def test_anual_plan_quote_discount_and_dates():
    """Plan ANUAL aplica 10% de descuento sobre 12 meses y 72h de gracia."""
    start = date(2026, 9, 15)
    quote = SubscriptionService.calculate_quote("ANUAL", start_date=start)

    assert quote.plan == "ANUAL"
    assert quote.months == 12
    assert quote.gross_total == Decimal("600000.00")
    assert quote.discount_rate == Decimal("10.00")
    assert quote.discount_amount == Decimal("60000.00")
    assert quote.final_price == Decimal("540000.00")  # $600,000 - 10%

    assert quote.cutoff_date == date(2027, 9, 15)  # 1 año exacto
    assert quote.grace_period_end == date(2027, 9, 18)  # +3 días (72h)


def test_mensual_plan_quote_no_discount():
    """Plan MENSUAL aplica tarifa base sin descuento comercial."""
    start = date(2026, 9, 15)
    quote = SubscriptionService.calculate_quote("MENSUAL", start_date=start)

    assert quote.plan == "MENSUAL"
    assert quote.months == 1
    assert quote.gross_total == Decimal("50000.00")
    assert quote.discount_rate == Decimal("0.00")
    assert quote.discount_amount == Decimal("0.00")
    assert quote.final_price == Decimal("50000.00")
    assert quote.cutoff_date == date(2026, 10, 15)
    assert quote.grace_period_end == date(2026, 10, 18)


def test_invalid_plan_raises_value_error():
    """Planes no contemplados en la matriz comercial lanzan ValueError."""
    with pytest.raises(ValueError) as exc_info:
        SubscriptionService.calculate_quote("QUINCENAL")
    assert "no válido" in str(exc_info.value)


def test_create_subscription_in_database(db_session_factory):
    """Crea y persiste un registro de suscripción consistente en DB."""
    db = db_session_factory()
    try:
        sub = SubscriptionService.create_subscription(
            client_id="usr-test-sub-client",
            plan="SEMESTRAL",
            start_date=date(2026, 9, 1),
            db=db,
        )

        assert sub.id is not None
        assert sub.client_id == "usr-test-sub-client"
        assert sub.plan == "SEMESTRAL"
        assert sub.discount_rate == Decimal("8.00")
        assert sub.final_price == Decimal("276000.00")
        assert sub.status == "ACTIVO"
        assert sub.start_date == date(2026, 9, 1)
        assert sub.cutoff_date == date(2027, 3, 1)
        assert sub.grace_period_end == date(2027, 3, 4)

        # Relación en User
        user = db.query(User).filter(User.id == "usr-test-sub-client").first()
        assert len(user.subscriptions) == 1
        assert user.subscriptions[0].id == sub.id
    finally:
        db.close()


def test_create_subscription_nonexistent_user_error(db_session_factory):
    """Falla si se intenta crear una suscripción para un cliente inexistente."""
    db = db_session_factory()
    try:
        with pytest.raises(ValueError) as exc_info:
            SubscriptionService.create_subscription(
                client_id="usr-no-existe",
                plan="ANUAL",
                db=db,
            )
        assert "No existe usuario" in str(exc_info.value)
    finally:
        db.close()


def test_renew_subscription_extends_period(db_session_factory):
    """Renovación de suscripción extiende el periodo de corte conservando continuidad."""
    db = db_session_factory()
    try:
        # Suscripción inicial Semestral
        sub = SubscriptionService.create_subscription(
            client_id="usr-test-sub-client",
            plan="SEMESTRAL",
            start_date=date.today(),
            db=db,
        )
        initial_cutoff = sub.cutoff_date

        # Renovar cambiando a Anual
        renewed = SubscriptionService.renew_subscription(
            subscription_id=sub.id,
            new_plan="ANUAL",
            db=db,
        )

        assert renewed.id == sub.id
        assert renewed.plan == "ANUAL"
        assert renewed.discount_rate == Decimal("10.00")
        assert renewed.final_price == Decimal("540000.00")
        # El corte debe haberse extendido 12 meses a partir del corte inicial
        expected_new_cutoff = add_months_to_date(initial_cutoff, 12)
        assert renewed.cutoff_date == expected_new_cutoff
        assert renewed.grace_period_end == expected_new_cutoff + timedelta(days=3)
    finally:
        db.close()


def test_grace_period_evaluation_states():
    """Evalúa los tres estados del ciclo: Al día, En Gracia (72h) y Bloqueado."""
    dummy_sub = Subscription(
        id="sub-dummy",
        client_id="usr-dummy",
        plan="TRIMESTRAL",
        cutoff_date=date(2026, 10, 1),
        grace_period_end=date(2026, 10, 4),  # 72h gracia
        status="ACTIVO",
    )

    # 1. Antes del corte: Al día
    status_active = SubscriptionService.get_grace_status(dummy_sub, check_date=date(2026, 9, 25))
    assert status_active["state"] == "ACTIVE"
    assert not status_active["is_in_grace"]
    assert not status_active["is_blocked"]
    assert status_active["days_until_cutoff"] == 6

    # 2. Día 1 de gracia (mismo día de corte)
    status_grace_1 = SubscriptionService.get_grace_status(dummy_sub, check_date=date(2026, 10, 1))
    assert status_grace_1["state"] == "IN_GRACE"
    assert status_grace_1["is_in_grace"] is True
    assert status_grace_1["day_of_grace"] == 1
    assert status_grace_1["days_left_in_grace"] == 3

    # 3. Día 2 de gracia
    status_grace_2 = SubscriptionService.get_grace_status(dummy_sub, check_date=date(2026, 10, 2))
    assert status_grace_2["state"] == "IN_GRACE"
    assert status_grace_2["day_of_grace"] == 2
    assert status_grace_2["days_left_in_grace"] == 2

    # 4. Día 3 de gracia (último día antes de bloqueo)
    status_grace_3 = SubscriptionService.get_grace_status(dummy_sub, check_date=date(2026, 10, 3))
    assert status_grace_3["state"] == "IN_GRACE"
    assert status_grace_3["day_of_grace"] == 3
    assert status_grace_3["days_left_in_grace"] == 1

    # 5. Pasado el periodo de gracia: Bloqueado
    status_blocked = SubscriptionService.get_grace_status(dummy_sub, check_date=date(2026, 10, 5))
    assert status_blocked["state"] == "BLOCKED"
    assert status_blocked["is_blocked"] is True
    assert status_blocked["days_overdue"] == 1
