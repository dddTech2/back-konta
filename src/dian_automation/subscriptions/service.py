"""Motor de Suscripciones y Tarifas de Konta.

Implementa la gestión de contratos de suscripción, cotizaciones y periodos de gracia.
Delega las tarifas, descuentos y vigencias en PricingService (Story 9.1).
"""

import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session

from dian_automation.db.models import User, Business, Subscription, PRICE_ORIGIN_ESPECIAL, PRICE_ORIGIN_TARIFA

logger = logging.getLogger("subscriptions")


def add_months_to_date(source_date: date, months: int) -> date:
    """Calcula una fecha futura sumando un número exacto de meses respetando días de fin de mes."""
    total_months = source_date.month - 1 + months
    year = source_date.year + total_months // 12
    month = total_months % 12 + 1
    max_days_in_target_month = calendar.monthrange(year, month)[1]
    day = min(source_date.day, max_days_in_target_month)
    return date(year, month, day)


@dataclass
class PlanQuote:
    """Detalle de cotización de suscripción con desglose de tarifas."""
    plan: str
    months: int
    base_monthly_price: Decimal
    gross_total: Decimal
    discount_rate: Decimal
    discount_amount: Decimal
    final_price: Decimal
    start_date: date
    cutoff_date: date
    grace_period_end: date
    monthly_price: Optional[Decimal] = None

    def __post_init__(self):
        if self.monthly_price is None:
            self.monthly_price = self.base_monthly_price

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan": self.plan,
            "months": self.months,
            "base_monthly_price": float(self.base_monthly_price),
            "monthly_price": float(self.monthly_price if self.monthly_price is not None else self.base_monthly_price),
            "gross_total": float(self.gross_total),
            "discount_rate": float(self.discount_rate),
            "discount_amount": float(self.discount_amount),
            "final_price": float(self.final_price),
            "start_date": self.start_date.isoformat(),
            "cutoff_date": self.cutoff_date.isoformat(),
            "grace_period_end": self.grace_period_end.isoformat(),
        }


class SubscriptionService:
    """Servicio de dominio para cálculo de tarifas, alta y renovación de suscripciones."""

    @classmethod
    def get_supported_plans(cls, db: Optional[Session] = None) -> List[str]:
        from dian_automation.subscriptions.pricing import PricingService
        from dian_automation.db.database import SessionLocal

        session_to_close = None
        session = db
        if session is None:
            session = SessionLocal()
            session_to_close = session
        try:
            return [p.code for p in PricingService.list_plans(session, active_only=True)]
        finally:
            if session_to_close:
                session_to_close.close()

    @classmethod
    def calculate_quote(
        cls,
        plan: str,
        start_date: Optional[date] = None,
        base_monthly_price: Optional[Decimal] = None,
        db: Optional[Session] = None,
    ) -> PlanQuote:
        """Calcula el presupuesto de un plan delegando en PricingService."""
        from dian_automation.subscriptions.pricing import PricingService
        from dian_automation.db.database import SessionLocal

        session_to_close = None
        session = db
        if session is None:
            session = SessionLocal()
            session_to_close = session

        try:
            plan_obj = PricingService.get_plan(session, plan, active_only=True)
            grace_days = PricingService.get_grace_days(session)

            if base_monthly_price is not None:
                return PricingService.quote(
                    monthly_price=base_monthly_price,
                    months=plan_obj.months,
                    discount_rate=plan_obj.discount_rate,
                    start_date=start_date,
                    grace_days=grace_days,
                    plan_code=plan_obj.code,
                )
            else:
                # Usa tarifa vigente por defecto
                rate = PricingService.current_rate(session, "DIAN", "PERSONA_NATURAL", on=start_date)
                return PricingService.quote(
                    monthly_price=rate.monthly_price,
                    months=plan_obj.months,
                    discount_rate=plan_obj.discount_rate,
                    start_date=start_date,
                    grace_days=grace_days,
                    plan_code=plan_obj.code,
                )
        finally:
            if session_to_close:
                session_to_close.close()

    @classmethod
    def create_subscription(
        cls,
        client_id: str,
        plan: str,
        start_date: Optional[date] = None,
        base_monthly_price: Optional[Decimal] = None,
        db: Optional[Session] = None,
    ) -> Subscription:
        """Crea y persiste un nuevo contrato de suscripción para un cliente."""
        if db is None:
            raise ValueError("Se requiere una sesión de base de datos activa.")

        from dian_automation.subscriptions.pricing import PricingService

        # Verificar existencia del cliente
        client = db.query(User).filter(User.id == client_id).first()
        if not client:
            raise ValueError(f"No existe usuario registrado con client_id='{client_id}'.")

        biz = (
            db.query(Business)
            .filter(Business.client_id == client.id, Business.is_active.is_(True))
            .order_by(Business.created_at.asc())
            .first()
        )
        if not biz:
            biz = (
                db.query(Business)
                .filter(Business.client_id == client.id)
                .order_by(Business.created_at.asc())
                .first()
            )

        inc_source = biz.income_source if biz else "DIAN"
        tx_type = biz.taxpayer_type if biz else "PERSONA_NATURAL"

        plan_obj = PricingService.get_plan(db, plan, active_only=True)
        grace_days = PricingService.get_grace_days(db)

        if base_monthly_price is not None:
            price_origin = PRICE_ORIGIN_ESPECIAL
            quote = PricingService.quote(
                monthly_price=base_monthly_price,
                months=plan_obj.months,
                discount_rate=plan_obj.discount_rate,
                start_date=start_date,
                grace_days=grace_days,
                plan_code=plan_obj.code,
            )
        else:
            price_origin = PRICE_ORIGIN_TARIFA
            rate = PricingService.current_rate(db, inc_source, tx_type, on=start_date)
            quote = PricingService.quote(
                monthly_price=rate.monthly_price,
                months=plan_obj.months,
                discount_rate=plan_obj.discount_rate,
                start_date=start_date,
                grace_days=grace_days,
                plan_code=plan_obj.code,
            )

        subscription = Subscription(
            client_id=client.id,
            plan=quote.plan,
            monthly_price=quote.monthly_price,
            price_origin=price_origin,
            discount_rate=quote.discount_rate,
            base_price=quote.gross_total,
            final_price=quote.final_price,
            start_date=quote.start_date,
            cutoff_date=quote.cutoff_date,
            grace_period_end=quote.grace_period_end,
            status="ACTIVO",
        )

        db.add(subscription)
        db.commit()
        db.refresh(subscription)

        logger.info(
            f"Suscripción {subscription.id} creada para cliente {client.id} (Plan: {subscription.plan}, "
            f"Valor: ${float(subscription.final_price):,.0f} COP, Corte: {subscription.cutoff_date})"
        )

        return subscription

    @classmethod
    def renew_subscription(
        cls,
        subscription_id: str,
        new_plan: Optional[str] = None,
        base_monthly_price: Optional[Decimal] = None,
        db: Optional[Session] = None,
    ) -> Subscription:
        """Renueva una suscripción existente extendiendo el periodo desde la fecha de corte o fecha actual."""
        if db is None:
            raise ValueError("Se requiere una sesión de base de datos activa.")

        from dian_automation.subscriptions.pricing import PricingService

        sub = db.query(Subscription).filter(Subscription.id == subscription_id).first()
        if not sub:
            raise ValueError(f"Suscripción con id='{subscription_id}' no encontrada.")

        plan_to_use = (new_plan or sub.plan).upper().strip()
        plan_obj = PricingService.get_plan(db, plan_to_use)
        today = date.today()
        renewal_start = sub.cutoff_date if sub.cutoff_date >= today else today
        grace_days = PricingService.get_grace_days(db)

        if base_monthly_price is not None:
            quote = PricingService.quote(
                monthly_price=base_monthly_price,
                months=plan_obj.months,
                discount_rate=plan_obj.discount_rate,
                start_date=renewal_start,
                grace_days=grace_days,
                plan_code=plan_obj.code,
            )
            sub.price_origin = PRICE_ORIGIN_ESPECIAL
            sub.monthly_price = quote.monthly_price
        elif sub.monthly_price is not None:
            quote = PricingService.quote(
                monthly_price=sub.monthly_price,
                months=plan_obj.months,
                discount_rate=plan_obj.discount_rate,
                start_date=renewal_start,
                grace_days=grace_days,
                plan_code=plan_obj.code,
            )
        else:
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
            inc_source = biz.income_source if biz else "DIAN"
            tx_type = biz.taxpayer_type if biz else "PERSONA_NATURAL"
            rate = PricingService.current_rate(db, inc_source, tx_type, on=renewal_start)
            quote = PricingService.quote(
                monthly_price=rate.monthly_price,
                months=plan_obj.months,
                discount_rate=plan_obj.discount_rate,
                start_date=renewal_start,
                grace_days=grace_days,
                plan_code=plan_obj.code,
            )
            sub.monthly_price = rate.monthly_price

        sub.plan = quote.plan
        sub.discount_rate = quote.discount_rate
        sub.base_price = quote.gross_total
        sub.final_price = quote.final_price
        sub.cutoff_date = quote.cutoff_date
        sub.grace_period_end = quote.grace_period_end
        sub.status = "ACTIVO"
        sub.last_notified_at = None

        db.commit()
        db.refresh(sub)

        logger.info(
            f"Suscripción {sub.id} renovada hasta {sub.cutoff_date} (Gracia hasta {sub.grace_period_end})"
        )
        return sub

    @classmethod
    def get_grace_status(cls, subscription: Subscription, check_date: Optional[date] = None) -> Dict[str, Any]:
        """Evalúa el estado de mora y periodo de gracia de una suscripción."""
        target_date = check_date or date.today()
        total_grace_days = (
            (subscription.grace_period_end - subscription.cutoff_date).days
            if (subscription.grace_period_end and subscription.cutoff_date)
            else 3
        )

        if target_date < subscription.cutoff_date:
            days_until_cutoff = (subscription.cutoff_date - target_date).days
            return {
                "state": "ACTIVE",
                "is_in_grace": False,
                "is_blocked": False,
                "days_until_cutoff": days_until_cutoff,
                "days_left_in_grace": total_grace_days,
                "message": f"Suscripción al día. Próximo corte en {days_until_cutoff} días.",
            }

        elif subscription.cutoff_date <= target_date <= subscription.grace_period_end:
            days_into_grace = (target_date - subscription.cutoff_date).days + 1
            days_left = (subscription.grace_period_end - target_date).days
            return {
                "state": "IN_GRACE",
                "is_in_grace": True,
                "is_blocked": False,
                "day_of_grace": days_into_grace,
                "days_left_in_grace": days_left,
                "message": (
                    f"Periodo de gracia activo (Día {days_into_grace} de {total_grace_days}). "
                    f"Quedan {days_left} días antes de suspensión."
                ),
            }

        else:
            days_overdue = (target_date - subscription.grace_period_end).days
            return {
                "state": "BLOCKED",
                "is_in_grace": False,
                "is_blocked": True,
                "days_overdue": days_overdue,
                "days_left_in_grace": 0,
                "message": f"Gracia vencida hace {days_overdue} días. Servicio suspendido.",
            }
