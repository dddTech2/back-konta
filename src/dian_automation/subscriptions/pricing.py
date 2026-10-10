"""Servicio de Precios y Tarifas Dinámicas de Konta (Story 9.1).

Centraliza las tarifas guardadas en base de datos, el cálculo de presupuestos con
descuentos, redondeo comercial a pesos enteros y el congelamiento del precio en cada suscripción.
"""

import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any
from zoneinfo import ZoneInfo
from sqlalchemy import func
from sqlalchemy.orm import Session

from dian_automation.db.models import (
    Business,
    BillingSetting,
    PricingPlan,
    PricingRate,
    Subscription,
    SubscriptionPriceChange,
    User,
    generate_uuid,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    PRICE_ORIGIN_ESPECIAL,
    PRICE_ORIGIN_TARIFA,
    TAXPAYER_TYPE_PERSONA_JURIDICA,
    TAXPAYER_TYPE_PERSONA_NATURAL,
)
from dian_automation.subscriptions.service import PlanQuote, add_months_to_date

logger = logging.getLogger("pricing")

MAX_MONTHLY_PRICE = Decimal("10000000")


class PricingError(Exception):
    """Excepción de dominio para errores en tarifas y precios con código tipado y status HTTP."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def ensure_default_pricing(db: Session) -> None:
    """Garantiza la presencia de la semilla de planes, tarifas y configuración de facturación.

    Idempotente: si pricing_plans y pricing_rates están vacías inserta la semilla de AC #1–#2;
    si falta la fila de billing_settings, la crea con grace_days = 3. Solo hace `flush` (nunca commit):
    se llama en medio de operaciones de quien llama (p. ej. `create_client`) y no debe confirmar su
    transacción a medias; la semilla queda guardada cuando quien llama hace commit.
    """
    seeded = False
    plans_count = db.query(PricingPlan).count()
    rates_count = db.query(PricingRate).count()

    if plans_count == 0 and rates_count == 0:
        seeded = True
        now_dt = datetime.utcnow()
        default_plans = [
            PricingPlan(
                code="MENSUAL",
                name="Mensual",
                months=1,
                discount_rate=Decimal("0.00"),
                is_active=False,
                sort_order=1,
                created_at=now_dt,
                updated_at=now_dt,
            ),
            PricingPlan(
                code="TRIMESTRAL",
                name="Trimestral",
                months=3,
                discount_rate=Decimal("5.00"),
                is_active=True,
                sort_order=2,
                created_at=now_dt,
                updated_at=now_dt,
            ),
            PricingPlan(
                code="SEMESTRAL",
                name="Semestral",
                months=6,
                discount_rate=Decimal("8.00"),
                is_active=True,
                sort_order=3,
                created_at=now_dt,
                updated_at=now_dt,
            ),
            PricingPlan(
                code="ANUAL",
                name="Anual",
                months=12,
                discount_rate=Decimal("10.00"),
                is_active=True,
                sort_order=4,
                created_at=now_dt,
                updated_at=now_dt,
            ),
        ]
        db.add_all(default_plans)

        eff_date = date(2026, 1, 1)
        default_rates = [
            PricingRate(
                id=generate_uuid(),
                income_source=INCOME_SOURCE_DIAN,
                taxpayer_type=TAXPAYER_TYPE_PERSONA_NATURAL,
                monthly_price=Decimal("50000.00"),
                effective_from=eff_date,
                created_by_admin_id=None,
                created_at=now_dt,
            ),
            PricingRate(
                id=generate_uuid(),
                income_source=INCOME_SOURCE_DIAN,
                taxpayer_type=TAXPAYER_TYPE_PERSONA_JURIDICA,
                monthly_price=Decimal("50000.00"),
                effective_from=eff_date,
                created_by_admin_id=None,
                created_at=now_dt,
            ),
            PricingRate(
                id=generate_uuid(),
                income_source=INCOME_SOURCE_MANUAL_SALES,
                taxpayer_type=TAXPAYER_TYPE_PERSONA_NATURAL,
                monthly_price=Decimal("50000.00"),
                effective_from=eff_date,
                created_by_admin_id=None,
                created_at=now_dt,
            ),
            PricingRate(
                id=generate_uuid(),
                income_source=INCOME_SOURCE_MANUAL_SALES,
                taxpayer_type=TAXPAYER_TYPE_PERSONA_JURIDICA,
                monthly_price=Decimal("50000.00"),
                effective_from=eff_date,
                created_by_admin_id=None,
                created_at=now_dt,
            ),
        ]
        db.add_all(default_rates)

    settings = db.query(BillingSetting).filter(BillingSetting.id == 1).first()
    if not settings:
        seeded = True
        db.add(BillingSetting(id=1, grace_days=3, updated_at=datetime.utcnow()))

    if seeded:
        db.flush()


class PricingService:
    """Servicio de dominio para planes, tarifas vigentes, cotizaciones y ajustes de precios."""

    @classmethod
    def list_plans(cls, db: Session, active_only: bool = False) -> List[PricingPlan]:
        """Lista los planes ordenados por sort_order; opcionalmente filtrados por activos."""
        ensure_default_pricing(db)
        query = db.query(PricingPlan).order_by(PricingPlan.sort_order.asc())
        if active_only:
            query = query.filter(PricingPlan.is_active.is_(True))
        return query.all()

    @classmethod
    def get_plan(cls, db: Session, code: str, active_only: bool = False) -> PricingPlan:
        """Obtiene un plan por código. Lanza PricingError('INVALID_PLAN', 400) si no existe o está inactivo."""
        ensure_default_pricing(db)
        plan_code = (code or "").upper().strip()
        plan = db.query(PricingPlan).filter(PricingPlan.code == plan_code).first()
        if not plan:
            raise PricingError("INVALID_PLAN", f"Plan '{code}' no existe.", 400)
        if active_only and not plan.is_active:
            raise PricingError("INVALID_PLAN", f"Plan '{code}' no está activo.", 400)
        return plan

    @classmethod
    def create_plan(
        cls,
        db: Session,
        code: str,
        name: str,
        months: int,
        discount_rate: Any,
        admin: Optional[User] = None,
    ) -> PricingPlan:
        """Crea un nuevo plan activo. Valida formato de código, meses 1–36 y descuento 0–50."""
        ensure_default_pricing(db)
        code_clean = (code or "").strip().upper()
        if not re.match(r"^[A-Z][A-Z_]{1,19}$", code_clean):
            raise PricingError(
                "INVALID_PLAN_CODE",
                "El código del plan debe tener entre 2 y 20 caracteres (mayúsculas y guiones bajos).",
                400,
            )

        try:
            months_val = int(months)
        except (ValueError, TypeError):
            raise PricingError("INVALID_MONTHS", "La duración en meses debe ser un número entero.", 400)

        if not (1 <= months_val <= 36):
            raise PricingError("INVALID_MONTHS", "La duración del plan debe estar entre 1 y 36 meses.", 400)

        try:
            disc_val = Decimal(str(discount_rate))
        except Exception:
            raise PricingError("INVALID_DISCOUNT_RATE", "El porcentaje de descuento debe ser numérico.", 400)

        if not (Decimal("0.00") <= disc_val <= Decimal("50.00")):
            raise PricingError("INVALID_DISCOUNT_RATE", "El descuento debe estar entre 0% y 50%.", 400)

        name_clean = (name or "").strip()
        if not name_clean:
            raise PricingError("INVALID_NAME", "El nombre visible del plan es obligatorio.", 400)

        existing = db.query(PricingPlan).filter(PricingPlan.code == code_clean).first()
        if existing:
            raise PricingError("PLAN_EXISTS", f"Ya existe un plan con el código '{code_clean}'.", 409)

        max_order = db.query(func.max(PricingPlan.sort_order)).scalar() or 0
        new_plan = PricingPlan(
            code=code_clean,
            name=name_clean,
            months=months_val,
            discount_rate=disc_val,
            is_active=True,
            sort_order=max_order + 1,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(new_plan)
        db.commit()
        db.refresh(new_plan)
        return new_plan

    @classmethod
    def update_plan(
        cls,
        db: Session,
        code: str,
        *,
        name: Optional[str] = None,
        discount_rate: Optional[Any] = None,
        is_active: Optional[bool] = None,
        admin: Optional[User] = None,
    ) -> PricingPlan:
        """Actualiza un plan existente. No acepta months y no permite desactivar el último plan activo."""
        ensure_default_pricing(db)
        plan = cls.get_plan(db, code)

        if is_active is False and plan.is_active:
            active_count = db.query(PricingPlan).filter(
                PricingPlan.is_active.is_(True),
                PricingPlan.code != plan.code,
            ).count()
            if active_count == 0:
                raise PricingError("LAST_ACTIVE_PLAN", "No se puede desactivar el último plan activo.", 409)
            plan.is_active = False
        elif is_active is True:
            plan.is_active = True

        if name is not None:
            name_clean = name.strip()
            if not name_clean:
                raise PricingError("INVALID_NAME", "El nombre visible del plan no puede estar vacío.", 400)
            plan.name = name_clean

        if discount_rate is not None:
            try:
                disc_val = Decimal(str(discount_rate))
            except Exception:
                raise PricingError("INVALID_DISCOUNT_RATE", "El porcentaje de descuento debe ser numérico.", 400)
            if not (Decimal("0.00") <= disc_val <= Decimal("50.00")):
                raise PricingError("INVALID_DISCOUNT_RATE", "El descuento debe estar entre 0% y 50%.", 400)
            plan.discount_rate = disc_val

        plan.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(plan)
        return plan

    @classmethod
    def get_grace_days(cls, db: Session) -> int:
        """Obtiene el número actual de días de gracia desde billing_settings (semilla 3)."""
        ensure_default_pricing(db)
        settings = db.query(BillingSetting).filter(BillingSetting.id == 1).first()
        return settings.grace_days if settings else 3

    @classmethod
    def set_grace_days(cls, db: Session, days: int, admin: Optional[User] = None) -> int:
        """Actualiza los días de gracia globales (1–15). Aplica a los próximos cortes."""
        ensure_default_pricing(db)
        try:
            days_val = int(days)
        except (ValueError, TypeError):
            raise PricingError("INVALID_GRACE_DAYS", "Los días de gracia deben ser un número entero.", 400)

        if not (1 <= days_val <= 15):
            raise PricingError("INVALID_GRACE_DAYS", "Los días de gracia deben estar entre 1 y 15.", 400)

        settings = db.query(BillingSetting).filter(BillingSetting.id == 1).first()
        if not settings:
            settings = BillingSetting(id=1, grace_days=days_val, updated_at=datetime.utcnow())
            db.add(settings)
        else:
            settings.grace_days = days_val
            settings.updated_at = datetime.utcnow()
            settings.updated_by_admin_id = admin.id if admin else None

        db.commit()
        return days_val

    @classmethod
    def current_rate(
        cls,
        db: Session,
        income_source: str,
        taxpayer_type: str,
        on: Optional[date] = None,
    ) -> PricingRate:
        """Obtiene la tarifa vigente (effective_from <= on) para el segmento dado."""
        ensure_default_pricing(db)
        target_date = on or datetime.now(ZoneInfo("America/Bogota")).date()
        rate = (
            db.query(PricingRate)
            .filter(
                PricingRate.income_source == income_source,
                PricingRate.taxpayer_type == taxpayer_type,
                PricingRate.effective_from <= target_date,
            )
            .order_by(PricingRate.effective_from.desc())
            .first()
        )
        if not rate:
            raise PricingError(
                "NO_RATE",
                f"No hay tarifa vigente para {income_source} y {taxpayer_type} a la fecha {target_date}.",
                409,
            )
        return rate

    @classmethod
    def quote(
        cls,
        monthly_price: Any,
        months: int,
        discount_rate: Any,
        start_date: Optional[date] = None,
        grace_days: int = 3,
        plan_code: str = "",
    ) -> PlanQuote:
        """Calcula el presupuesto con redondeo a pesos enteros en final_price."""
        s_date = start_date or datetime.now(ZoneInfo("America/Bogota")).date()
        m_price = Decimal(str(monthly_price))
        d_rate = Decimal(str(discount_rate))
        months_int = int(months)

        gross_total = (m_price * Decimal(months_int)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        disc_raw = gross_total * (d_rate / Decimal("100.00"))
        # Redondeo a pesos enteros (ROUND_HALF_UP, Decimal('1'))
        final_price = (gross_total - disc_raw).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        discount_amount = gross_total - final_price

        cutoff_date = add_months_to_date(s_date, months_int)
        grace_period_end = cutoff_date + timedelta(days=grace_days)

        return PlanQuote(
            plan=plan_code,
            months=months_int,
            base_monthly_price=m_price,
            gross_total=gross_total,
            discount_rate=d_rate,
            discount_amount=discount_amount,
            final_price=final_price,
            start_date=s_date,
            cutoff_date=cutoff_date,
            grace_period_end=grace_period_end,
            monthly_price=m_price,
        )

    @classmethod
    def quote_for_segment(
        cls,
        db: Session,
        income_source: str,
        taxpayer_type: str,
        plan_code: str,
        start_date: Optional[date] = None,
    ) -> PlanQuote:
        """Calcula el presupuesto combinando la tarifa vigente del segmento y el descuento del plan activo."""
        plan = cls.get_plan(db, plan_code, active_only=True)
        s_date = start_date or datetime.now(ZoneInfo("America/Bogota")).date()
        rate = cls.current_rate(db, income_source, taxpayer_type, on=s_date)
        grace_days = cls.get_grace_days(db)
        return cls.quote(
            monthly_price=rate.monthly_price,
            months=plan.months,
            discount_rate=plan.discount_rate,
            start_date=s_date,
            grace_days=grace_days,
            plan_code=plan.code,
        )

    @classmethod
    def apply_to_subscription(
        cls,
        db: Session,
        sub: Subscription,
        *,
        plan_code: Optional[str] = None,
        monthly_price: Optional[Any] = None,
        discount_rate: Optional[Any] = None,
        use_current_rate: bool = False,
        reason: str,
        admin: Optional[User] = None,
    ) -> Subscription:
        """Aplica un cambio de tarifa o plan a una suscripción existente sin alterar sus fechas de corte.

        Escribe una fila en subscription_price_changes y no realiza commit (lo hace el llamador).
        """
        reason_clean = (reason or "").strip()
        if not reason_clean:
            raise PricingError("REASON_REQUIRED", "El motivo del cambio de precio es obligatorio.", 400)

        if use_current_rate and (monthly_price is not None or discount_rate is not None):
            raise PricingError(
                "INVALID_PRICE_CHANGE",
                "Volver a la tarifa vigente no se combina con un precio o descuento especial.",
                400,
            )
        if monthly_price is not None:
            try:
                monthly_price = Decimal(str(monthly_price))
            except Exception:
                raise PricingError("INVALID_MONTHLY_PRICE", "El valor mensual debe ser numérico.", 400)
            if not (Decimal("0") < monthly_price <= MAX_MONTHLY_PRICE):
                raise PricingError(
                    "INVALID_MONTHLY_PRICE", "El valor mensual debe ser mayor a 0 y máximo $10.000.000.", 400
                )
        if discount_rate is not None:
            try:
                discount_rate = Decimal(str(discount_rate))
            except Exception:
                raise PricingError("INVALID_DISCOUNT_RATE", "El porcentaje de descuento debe ser numérico.", 400)
            if not (Decimal("0.00") <= discount_rate <= Decimal("50.00")):
                raise PricingError("INVALID_DISCOUNT_RATE", "El descuento debe estar entre 0% y 50%.", 400)

        old_plan = sub.plan
        old_monthly_price = sub.monthly_price
        old_discount_rate = sub.discount_rate
        old_final_price = sub.final_price

        target_plan_code = (plan_code or sub.plan).upper().strip()
        # Pasar a otro plan exige que esté activo; el plan actual puede seguir aunque ya no se ofrezca.
        target_plan = cls.get_plan(db, target_plan_code, active_only=target_plan_code != sub.plan)
        # Suscripciones anteriores a la migración sin valor mensual: se deduce del periodo.
        current_monthly = sub.monthly_price
        if current_monthly is None:
            months_now = max(1, cls.get_plan(db, sub.plan).months) if sub.plan else 3
            current_monthly = (Decimal(str(sub.base_price)) / Decimal(months_now)).quantize(Decimal("0.01"))

        if monthly_price is not None or discount_rate is not None:
            new_price_origin = PRICE_ORIGIN_ESPECIAL
            new_monthly_price = monthly_price if monthly_price is not None else current_monthly
            new_discount_rate = discount_rate if discount_rate is not None else target_plan.discount_rate
            sub.price_note = reason_clean
        elif use_current_rate:
            new_price_origin = PRICE_ORIGIN_TARIFA
            biz = (
                db.query(Business)
                .filter(Business.client_id == sub.client_id, Business.is_active.is_(True))
                .order_by(Business.created_at.asc())
                .first()
            )
            if not biz:
                biz = (
                    db.query(Business)
                    .filter(Business.client_id == sub.client_id)
                    .order_by(Business.created_at.asc())
                    .first()
                )
            inc_src = biz.income_source if biz else INCOME_SOURCE_DIAN
            tx_type = biz.taxpayer_type if biz else TAXPAYER_TYPE_PERSONA_NATURAL
            rate = cls.current_rate(db, inc_src, tx_type)
            new_monthly_price = rate.monthly_price
            new_discount_rate = target_plan.discount_rate
            sub.price_note = None
        else:
            # Solo se especificó plan_code: conserva monthly_price y price_origin y toma descuento del plan
            new_price_origin = sub.price_origin or PRICE_ORIGIN_TARIFA
            new_monthly_price = current_monthly
            new_discount_rate = target_plan.discount_rate

        gross_total = (new_monthly_price * Decimal(target_plan.months)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        disc_raw = gross_total * (new_discount_rate / Decimal("100.00"))
        new_final_price = (gross_total - disc_raw).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

        sub.plan = target_plan.code
        sub.monthly_price = new_monthly_price
        sub.discount_rate = new_discount_rate
        sub.base_price = gross_total
        sub.final_price = new_final_price
        sub.price_origin = new_price_origin

        change = SubscriptionPriceChange(
            id=generate_uuid(),
            subscription_id=sub.id,
            old_plan=old_plan,
            new_plan=sub.plan,
            old_monthly_price=old_monthly_price,
            new_monthly_price=new_monthly_price,
            old_discount_rate=old_discount_rate,
            new_discount_rate=new_discount_rate,
            old_final_price=old_final_price,
            new_final_price=new_final_price,
            reason=reason_clean,
            admin_id=admin.id if admin else None,
            created_at=datetime.utcnow(),
        )
        db.add(change)

        return sub
