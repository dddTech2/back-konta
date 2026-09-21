"""Motor de Suscripciones y Tarifas de Konta.

Implementa la matriz de precios periódicos con descuentos comerciales automáticos:
- TRIMESTRAL: 3 meses, -5% descuento.
- SEMESTRAL: 6 meses, -8% descuento.
- ANUAL: 12 meses, -10% descuento.
- MENSUAL: 1 mes, 0% descuento.

Gestiona las fechas de corte y la ventana de gracia de 72 horas (3 días).
"""

import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session

from dian_automation.db.models import User, Subscription

logger = logging.getLogger("subscriptions")

DEFAULT_BASE_MONTHLY_PRICE = Decimal("50000.00")

PLAN_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "MENSUAL": {
        "months": 1,
        "discount_rate": Decimal("0.00"),
        "description": "Plan Mensual (Sin descuento)",
    },
    "TRIMESTRAL": {
        "months": 3,
        "discount_rate": Decimal("5.00"),
        "description": "Plan Trimestral (Ahorro del 5%)",
    },
    "SEMESTRAL": {
        "months": 6,
        "discount_rate": Decimal("8.00"),
        "description": "Plan Semestral (Ahorro del 8%)",
    },
    "ANUAL": {
        "months": 12,
        "discount_rate": Decimal("10.00"),
        "description": "Plan Anual (Ahorro del 10%)",
    },
}


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan": self.plan,
            "months": self.months,
            "base_monthly_price": float(self.base_monthly_price),
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
    def get_supported_plans(cls) -> List[str]:
        return list(PLAN_DEFINITIONS.keys())

    @classmethod
    def calculate_quote(
        cls,
        plan: str,
        start_date: Optional[date] = None,
        base_monthly_price: Decimal = DEFAULT_BASE_MONTHLY_PRICE,
    ) -> PlanQuote:
        """Calcula el presupuesto de un plan aplicando el descuento porcentual y las fechas límite."""
        plan_key = plan.upper().strip()
        if plan_key not in PLAN_DEFINITIONS:
            valid_options = ", ".join(PLAN_DEFINITIONS.keys())
            raise ValueError(f"Plan '{plan}' no válido. Opciones permitidas: {valid_options}")

        plan_cfg = PLAN_DEFINITIONS[plan_key]
        months = plan_cfg["months"]
        discount_rate = plan_cfg["discount_rate"]

        s_date = start_date or date.today()
        cutoff_date = add_months_to_date(s_date, months)
        # Periodo de gracia de 72 horas (3 días calendario completos)
        grace_period_end = cutoff_date + timedelta(days=3)

        gross_total = (base_monthly_price * months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        discount_amount = (gross_total * (discount_rate / Decimal("100.00"))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        final_price = (gross_total - discount_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return PlanQuote(
            plan=plan_key,
            months=months,
            base_monthly_price=base_monthly_price,
            gross_total=gross_total,
            discount_rate=discount_rate,
            discount_amount=discount_amount,
            final_price=final_price,
            start_date=s_date,
            cutoff_date=cutoff_date,
            grace_period_end=grace_period_end,
        )

    @classmethod
    def create_subscription(
        cls,
        client_id: str,
        plan: str,
        start_date: Optional[date] = None,
        base_monthly_price: Decimal = DEFAULT_BASE_MONTHLY_PRICE,
        db: Optional[Session] = None,
    ) -> Subscription:
        """Crea y persiste un nuevo contrato de suscripción para un cliente."""
        if db is None:
            raise ValueError("Se requiere una sesión de base de datos activa.")

        # Verificar existencia del cliente
        client = db.query(User).filter(User.id == client_id).first()
        if not client:
            raise ValueError(f"No existe usuario registrado con client_id='{client_id}'.")

        quote = cls.calculate_quote(plan, start_date=start_date, base_monthly_price=base_monthly_price)

        subscription = Subscription(
            client_id=client.id,
            plan=quote.plan,
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
        base_monthly_price: Decimal = DEFAULT_BASE_MONTHLY_PRICE,
        db: Optional[Session] = None,
    ) -> Subscription:
        """Renueva una suscripción existente extendiendo el periodo desde la fecha de corte o fecha actual."""
        if db is None:
            raise ValueError("Se requiere una sesión de base de datos activa.")

        sub = db.query(Subscription).filter(Subscription.id == subscription_id).first()
        if not sub:
            raise ValueError(f"Suscripción con id='{subscription_id}' no encontrada.")

        plan_to_use = (new_plan or sub.plan).upper().strip()
        today = date.today()

        # Si el corte actual sigue vigente en el futuro, renovar desde el corte; sino desde hoy
        renewal_start = sub.cutoff_date if sub.cutoff_date >= today else today

        quote = cls.calculate_quote(plan_to_use, start_date=renewal_start, base_monthly_price=base_monthly_price)

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

        if target_date < subscription.cutoff_date:
            days_until_cutoff = (subscription.cutoff_date - target_date).days
            return {
                "state": "ACTIVE",
                "is_in_grace": False,
                "is_blocked": False,
                "days_until_cutoff": days_until_cutoff,
                "days_left_in_grace": 3,
                "message": f"Suscripción al día. Próximo corte en {days_until_cutoff} días.",
            }

        elif subscription.cutoff_date <= target_date <= subscription.grace_period_end:
            days_into_grace = (target_date - subscription.cutoff_date).days + 1
            days_left = (subscription.grace_period_end - target_date).days
            return {
                "state": "IN_GRACE",
                "is_in_grace": True,
                "is_blocked": False,
                "day_of_grace": days_into_grace,  # Día 1, 2 o 3 de gracia
                "days_left_in_grace": days_left,
                "message": f"Periodo de gracia activo (Día {days_into_grace} de 3). Quedan {days_left} días antes de suspensión.",
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
