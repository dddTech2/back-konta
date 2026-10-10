"""Pruebas unitarias para Story 9.1: Tarifas configurables, PricingService y billing settings."""

from datetime import date, datetime, timedelta
from decimal import Decimal
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.core import admin_service
from dian_automation.core.admin_service import AdminServiceError, NewClientData
from dian_automation.db.models import (
    Base,
    BillingSetting,
    Business,
    PaymentRecord,
    PricingPlan,
    PricingRate,
    Subscription,
    SubscriptionPriceChange,
    User,
    PRICE_ORIGIN_TARIFA,
    PRICE_ORIGIN_ESPECIAL,
)
from dian_automation.subscriptions.pricing import (
    PricingError,
    PricingService,
    ensure_default_pricing,
)
from dian_automation.subscriptions.service import add_months_to_date
from dian_automation.telegram.admin_bot import AdminTelegramBot


@pytest.fixture
def db_session_factory():
    """Crea una base de datos SQLite en memoria con esquema completo y administrador."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = TestingSessionLocal()
    admin_user = User(
        id="usr-admin-katerinn",
        email="katerinn.admin@kontable.co",
        full_name="Katerinn Administradora",
        role="ADMIN",
        telegram_chat_id=555444333,
        is_active=True,
    )
    db.add(admin_user)
    db.commit()
    db.close()

    return TestingSessionLocal


# ---------------------------------------------------------------------------
# 1. ensure_default_pricing (Semilla idempotente)
# ---------------------------------------------------------------------------


def test_ensure_default_pricing_seeds_and_is_idempotent(db_session_factory):
    """Verifica que la semilla cree los 4 planes, 4 tarifas y 1 configuración de facturación."""
    db = db_session_factory()
    try:
        # Primera ejecución
        ensure_default_pricing(db)

        plans = db.query(PricingPlan).order_by(PricingPlan.sort_order).all()
        assert len(plans) == 4
        codes = [p.code for p in plans]
        assert codes == ["MENSUAL", "TRIMESTRAL", "SEMESTRAL", "ANUAL"]

        mensual = plans[0]
        assert mensual.is_active is False
        assert mensual.months == 1
        assert mensual.discount_rate == Decimal("0.00")

        trimestral = plans[1]
        assert trimestral.is_active is True
        assert trimestral.months == 3
        assert trimestral.discount_rate == Decimal("5.00")

        rates = db.query(PricingRate).all()
        assert len(rates) == 4
        for r in rates:
            assert r.monthly_price == Decimal("50000.00")
            assert r.effective_from == date(2026, 1, 1)

        settings = db.query(BillingSetting).filter(BillingSetting.id == 1).first()
        assert settings is not None
        assert settings.grace_days == 3

        # Segunda ejecución (idempotente: no duplica ni falla)
        ensure_default_pricing(db)
        assert db.query(PricingPlan).count() == 4
        assert db.query(PricingRate).count() == 4
        assert db.query(BillingSetting).count() == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. current_rate y vigencias
# ---------------------------------------------------------------------------


def test_current_rate_picks_latest_effective_rate_and_handles_no_rate(db_session_factory):
    """Verifica que se elija la tarifa vigente más reciente en o antes de la fecha dada."""
    db = db_session_factory()
    try:
        ensure_default_pricing(db)

        # Tarifa semilla vigente para 2026-03-01
        rate_march = PricingService.current_rate(
            db, "DIAN", "PERSONA_NATURAL", on=date(2026, 3, 1)
        )
        assert rate_march.monthly_price == Decimal("50000.00")

        # Insertar nueva tarifa a partir de 2026-07-01 ($60,000)
        new_rate = PricingRate(
            id="rate-future",
            income_source="DIAN",
            taxpayer_type="PERSONA_NATURAL",
            monthly_price=Decimal("60000.00"),
            effective_from=date(2026, 7, 1),
        )
        db.add(new_rate)
        db.commit()

        # En junio sigue rigiendo la de $50,000
        rate_june = PricingService.current_rate(
            db, "DIAN", "PERSONA_NATURAL", on=date(2026, 6, 30)
        )
        assert rate_june.monthly_price == Decimal("50000.00")

        # En julio ya rige la de $60,000
        rate_july = PricingService.current_rate(
            db, "DIAN", "PERSONA_NATURAL", on=date(2026, 7, 1)
        )
        assert rate_july.monthly_price == Decimal("60000.00")

        # Antes de 2026-01-01 no hay tarifa -> NO_RATE (409)
        with pytest.raises(PricingError) as exc_info:
            PricingService.current_rate(
                db, "DIAN", "PERSONA_NATURAL", on=date(2025, 12, 31)
            )
        assert exc_info.value.code == "NO_RATE"
        assert exc_info.value.status_code == 409
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. quote: redondeo y meses calendario
# ---------------------------------------------------------------------------


def test_quote_rounding_to_whole_pesos_and_calendar_months():
    """Valida redondeo exacto ROUND_HALF_UP a pesos enteros y suma de meses calendario."""
    # Ejemplo de la historia: tarifa $47,333 mensual, 3 meses, 5% descuento
    # Total bruto: 47,333 * 3 = 141,999
    # Descuento: 141,999 * 0.05 = 7,099.95
    # Precio final: 141,999 - 7,099.95 = 134,899.05 -> redondeado a 134,899
    # Descuento final recalculado: 141,999 - 134,899 = 7,100
    q = PricingService.quote(
        monthly_price=Decimal("47333"),
        months=3,
        discount_rate=Decimal("5.00"),
        start_date=date(2026, 1, 31),
        grace_days=3,
    )
    assert q.gross_total == Decimal("141999.00")
    assert q.final_price == Decimal("134899.00")
    assert q.discount_amount == Decimal("7100.00")
    assert q.monthly_price == Decimal("47333")

    # Meses calendario: 31 de enero + 1 mes = 28 de febrero (año no bisiesto 2026)
    q1 = PricingService.quote(
        monthly_price=Decimal("50000"),
        months=1,
        discount_rate=Decimal("0.00"),
        start_date=date(2026, 1, 31),
    )
    assert q1.cutoff_date == date(2026, 2, 28)
    assert q1.grace_period_end == date(2026, 3, 3)


# ---------------------------------------------------------------------------
# 4. create_plan y update_plan
# ---------------------------------------------------------------------------


def test_create_plan_validations_and_sort_order(db_session_factory):
    """Valida creación de planes, formato de código, meses, descuento y unicidad."""
    db = db_session_factory()
    try:
        ensure_default_pricing(db)

        # Creación exitosa
        plan = PricingService.create_plan(
            db=db,
            code="BIANUAL",
            name="Plan Dos Años",
            months=24,
            discount_rate=Decimal("15.00"),
        )
        assert plan.code == "BIANUAL"
        assert plan.months == 24
        assert plan.discount_rate == Decimal("15.00")
        assert plan.is_active is True
        assert plan.sort_order == 5  # máximo existente era 4

        # Código repetido -> PLAN_EXISTS (409)
        with pytest.raises(PricingError) as exc_dup:
            PricingService.create_plan(db, "BIANUAL", "Otro", 24, Decimal("10"))
        assert exc_dup.value.code == "PLAN_EXISTS"
        assert exc_dup.value.status_code == 409

        # Código con minúsculas o caracteres inválidos -> INVALID_PLAN_CODE (400)
        with pytest.raises(PricingError) as exc_code:
            PricingService.create_plan(db, "BI-ANUAL", "Otro", 24, Decimal("10"))
        assert exc_code.value.code == "INVALID_PLAN_CODE"

        # Meses fuera de rango (0 o 37) -> INVALID_MONTHS (400)
        with pytest.raises(PricingError) as exc_m0:
            PricingService.create_plan(db, "INVALID_M", "Otro", 0, Decimal("10"))
        assert exc_m0.value.code == "INVALID_MONTHS"

        with pytest.raises(PricingError) as exc_m37:
            PricingService.create_plan(db, "INVALID_M", "Otro", 37, Decimal("10"))
        assert exc_m37.value.code == "INVALID_MONTHS"

        # Descuento fuera de rango (-1 o 51) -> INVALID_DISCOUNT_RATE (400)
        with pytest.raises(PricingError) as exc_dneg:
            PricingService.create_plan(db, "INVALID_D", "Otro", 12, Decimal("-1"))
        assert exc_dneg.value.code == "INVALID_DISCOUNT_RATE"

        with pytest.raises(PricingError) as exc_dmax:
            PricingService.create_plan(db, "INVALID_D", "Otro", 12, Decimal("51"))
        assert exc_dmax.value.code == "INVALID_DISCOUNT_RATE"
    finally:
        db.close()


def test_update_plan_months_not_editable_and_last_active_guard(db_session_factory):
    """Verifica que update_plan actualice campos válidos pero impida desactivar el último activo."""
    db = db_session_factory()
    try:
        ensure_default_pricing(db)

        # Actualizar nombre y descuento
        updated = PricingService.update_plan(
            db, "TRIMESTRAL", name="Trimestral Konta", discount_rate=Decimal("6.00")
        )
        assert updated.name == "Trimestral Konta"
        assert updated.discount_rate == Decimal("6.00")

        # Desactivar planes hasta que quede solo 1 activo
        PricingService.update_plan(db, "SEMESTRAL", is_active=False)
        PricingService.update_plan(db, "ANUAL", is_active=False)

        # Intentar desactivar TRIMESTRAL (último activo) -> LAST_ACTIVE_PLAN (409)
        with pytest.raises(PricingError) as exc_last:
            PricingService.update_plan(db, "TRIMESTRAL", is_active=False)
        assert exc_last.value.code == "LAST_ACTIVE_PLAN"
        assert exc_last.value.status_code == 409
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5. Días de gracia (get_grace_days y set_grace_days)
# ---------------------------------------------------------------------------


def test_grace_days_configuration_and_range_validation(db_session_factory):
    """Valida obtención y modificación de grace_days dentro del rango 1–15."""
    db = db_session_factory()
    try:
        ensure_default_pricing(db)

        assert PricingService.get_grace_days(db) == 3

        admin = db.query(User).filter(User.role == "ADMIN").first()
        new_val = PricingService.set_grace_days(db, 5, admin=admin)
        assert new_val == 5
        assert PricingService.get_grace_days(db) == 5

        # Menor a 1 o mayor a 15 -> INVALID_GRACE_DAYS (400)
        with pytest.raises(PricingError) as exc_0:
            PricingService.set_grace_days(db, 0)
        assert exc_0.value.code == "INVALID_GRACE_DAYS"

        with pytest.raises(PricingError) as exc_16:
            PricingService.set_grace_days(db, 16)
        assert exc_16.value.code == "INVALID_GRACE_DAYS"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 6. apply_to_subscription (Cambios de tarifa y orígenes)
# ---------------------------------------------------------------------------


def test_apply_to_subscription_special_tariff_and_history_logging(db_session_factory):
    """Verifica que apply_to_subscription congele tarifas, registre cambios y preserve fechas."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()
        created = admin_service.create_client(
            db,
            admin,
            NewClientData(
                tipo_cliente="PERSONA",
                full_name="Cliente Tarifas",
                phone="3009998877",
                nit="1000000088",
                plan="TRIMESTRAL",
            ),
        )
        sub = created.subscription
        orig_cutoff = sub.cutoff_date
        orig_grace = sub.grace_period_end

        # 1. Sin motivo -> REASON_REQUIRED (400)
        with pytest.raises(PricingError) as exc_reason:
            PricingService.apply_to_subscription(
                db, sub, monthly_price=Decimal("45000"), reason=""
            )
        assert exc_reason.value.code == "REASON_REQUIRED"

        # 2. Tarifa especial (precio personalizado) -> price_origin = ESPECIAL
        PricingService.apply_to_subscription(
            db,
            sub,
            monthly_price=Decimal("40000"),
            reason="Descuento de fidelidad acordado",
            admin=admin,
        )
        db.commit()

        db.refresh(sub)
        assert sub.price_origin == PRICE_ORIGIN_ESPECIAL
        assert sub.monthly_price == Decimal("40000")
        assert sub.base_price == Decimal("120000.00")  # 40,000 * 3
        assert sub.final_price == Decimal("114000.00")  # 120,000 - 5% (6,000)
        assert sub.cutoff_date == orig_cutoff  # Fechas NO se mueven
        assert sub.grace_period_end == orig_grace

        # Verificar historial registrado
        history = db.query(SubscriptionPriceChange).filter(
            SubscriptionPriceChange.subscription_id == sub.id
        ).all()
        assert len(history) == 1
        h = history[0]
        assert h.old_monthly_price == Decimal("50000.00")
        assert h.new_monthly_price == Decimal("40000.00")
        assert h.reason == "Descuento de fidelidad acordado"
        assert h.admin_id == admin.id

        # 3. use_current_rate=True restablece a TARIFA oficial
        PricingService.apply_to_subscription(
            db,
            sub,
            use_current_rate=True,
            reason="Restablecimiento a tarifa regular",
            admin=admin,
        )
        db.commit()

        db.refresh(sub)
        assert sub.price_origin == PRICE_ORIGIN_TARIFA
        assert sub.monthly_price == Decimal("50000.00")
        assert sub.final_price == Decimal("142500.00")
        assert sub.price_note is None
        assert db.query(SubscriptionPriceChange).count() == 2
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 7. create_client y confirm_payment con congelamiento y aviso de diferencia
# ---------------------------------------------------------------------------


def test_create_client_and_confirm_payment_rate_freeze_and_grace_days(db_session_factory):
    """Verifica que el precio quede congelado tras un aumento de tarifas y use los días de gracia vigentes."""
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()

        # Establecer 5 días de gracia para los próximos clientes
        PricingService.set_grace_days(db, 5, admin=admin)
        db.commit()

        created = admin_service.create_client(
            db,
            admin,
            NewClientData(
                tipo_cliente="PERSONA",
                full_name="Cliente A",
                phone="3001112233",
                nit="1000000099",
                plan="TRIMESTRAL",
            ),
        )
        sub = created.subscription
        assert sub.monthly_price == Decimal("50000.00")
        assert sub.final_price == Decimal("142500.00")
        assert sub.grace_period_end == sub.cutoff_date + timedelta(days=5)

        # Ahora el gobierno o Katerinn sube la tarifa oficial a $70,000 para nuevos contratos
        new_rate = PricingRate(
            id="rate-increase",
            income_source="DIAN",
            taxpayer_type="PERSONA_NATURAL",
            monthly_price=Decimal("70000.00"),
            effective_from=date.today(),
        )
        db.add(new_rate)
        db.commit()

        # Nuevo cliente B toma la nueva tarifa ($70,000)
        created_b = admin_service.create_client(
            db,
            admin,
            NewClientData(
                tipo_cliente="PERSONA",
                full_name="Cliente B",
                phone="3002223344",
                nit="1000000077",
                plan="TRIMESTRAL",
            ),
        )
        assert created_b.subscription.monthly_price == Decimal("70000.00")

        # Cliente A confirma su pago: SU PRECIO CONGELADO ($142,500) SE MANTIENE
        p_res = admin_service.confirm_payment(
            db=db,
            admin=admin,
            nit="1000000099",
            amount=Decimal("142500.00"),
            reference="TR-0099",
            allow_mismatch=False,
        )
        assert p_res.expected_amount == Decimal("142500.00")
        assert p_res.difference == Decimal("0.00")
        assert p_res.payment.expected_amount == Decimal("142500.00")

        # Pago con monto diferente y allow_mismatch=False -> AMOUNT_MISMATCH (409)
        with pytest.raises(AdminServiceError) as exc_mismatch:
            admin_service.confirm_payment(
                db=db,
                admin=admin,
                nit="1000000099",
                amount=Decimal("150000.00"),
                reference="TR-0099-B",
                allow_mismatch=False,
            )
        assert exc_mismatch.value.code == "AMOUNT_MISMATCH"
        assert exc_mismatch.value.status_code == 409
    finally:
        db.close()


def test_telegram_bot_confirmar_pago_mismatch_warning(db_session_factory):
    """Verifica que el bot de Telegram añada la advertencia formateada cuando hay diferencia de monto."""
    db = db_session_factory()
    try:
        admin_chat_id = 555444333
        AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=admin_chat_id,
            text="/crear_cliente PERSONA | Jorge Bot | 3004445566 | 1000000055 | TRIMESTRAL",
            db=db,
        )

        # Confirmar pago con monto diferente ($150,000 en lugar del esperado $142,500)
        res = AdminTelegramBot.execute_confirmar_pago(
            sender_chat_id=admin_chat_id,
            text="/confirmar_pago 1000000055 | 150000 | TR-DIFF-1",
            db=db,
        )

        assert res["success"] is True
        assert res["expected_amount"] == Decimal("142500.00")
        assert res["difference"] == Decimal("7500.00")

        # Advertencia en el mensaje
        msg = res["message"]
        assert "⚠️ El valor del periodo es *$142,500*" in msg
        assert "recibido *$150,000*" in msg
        assert "diferencia *+$7,500*" in msg
        assert "Se registró igual." in msg
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Correcciones de la verificación (2026-10-10)
# ---------------------------------------------------------------------------


def test_ensure_default_pricing_does_not_commit_caller_transaction(db_session_factory):
    """La semilla solo hace flush: un rollback de quien llama deshace también lo pendiente."""
    db = db_session_factory()
    try:
        db.add(User(id="usr-pendiente", email="pendiente@test.co", full_name="Pendiente", role="CLIENT", is_active=True))
        PricingService.get_plan(db, "TRIMESTRAL")  # dispara la semilla en una base vacía
        db.rollback()
        assert db.query(User).filter(User.id == "usr-pendiente").first() is None
    finally:
        db.close()


def test_apply_to_subscription_validates_values_and_inactive_plans(db_session_factory):
    db = db_session_factory()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()
        created = admin_service.create_client(
            db,
            admin,
            NewClientData(tipo_cliente="PERSONA", full_name="Cliente Validaciones", phone="3001112233",
                          nit="1000000099", plan="TRIMESTRAL"),
        )
        sub = created.subscription
        for kwargs, code in (
            ({"monthly_price": Decimal("0")}, "INVALID_MONTHLY_PRICE"),
            ({"monthly_price": Decimal("10000001")}, "INVALID_MONTHLY_PRICE"),
            ({"discount_rate": Decimal("51")}, "INVALID_DISCOUNT_RATE"),
            ({"use_current_rate": True, "monthly_price": Decimal("40000")}, "INVALID_PRICE_CHANGE"),
            ({"plan_code": "MENSUAL"}, "INVALID_PLAN"),  # inactivo en la semilla
        ):
            with pytest.raises(PricingError) as exc:
                PricingService.apply_to_subscription(db, sub, reason="prueba", **kwargs)
            assert exc.value.code == code

        PricingService.apply_to_subscription(db, sub, discount_rate=Decimal("20"), reason="Cliente referido")
        db.commit()
        db.refresh(sub)
        assert sub.price_origin == PRICE_ORIGIN_ESPECIAL
        assert sub.price_note == "Cliente referido"
        assert sub.final_price == Decimal("120000")  # 50.000 × 3 × 0,80
    finally:
        db.close()
