"""Cron y tareas programadas para la gestión del periodo de gracia de 72 horas.

Identifica suscripciones que alcanzaron su fecha de corte sin pago,
las transiciona a estado 'EN_MORA', despacha recordatorios amigables y progresivos
por Telegram durante los días 1, 2 y 3 de gracia, y gestiona las alertas para el banner web.
"""

import logging
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List, Callable
from sqlalchemy.orm import Session

from dian_automation.db.models import Subscription, User

logger = logging.getLogger("grace_cron")


class SubscriptionGraceCron:
    """Orquestador del ciclo de vida de gracia y despacho de alertas de cobro."""

    @classmethod
    def get_grace_notification_message(
        cls,
        subscription: Subscription,
        user: User,
        business_name: str,
        day_of_grace: int,
        days_remaining: int,
    ) -> str:
        """Genera el mensaje correspondiente al día de gracia (1, 2 o 3)."""
        price_cop = float(subscription.final_price)
        cutoff_fmt = subscription.cutoff_date.strftime("%d/%m/%Y")
        grace_end_fmt = subscription.grace_period_end.strftime("%d/%m/%Y")

        if day_of_grace <= 1:
            return (
                "⚠️ *Recordatorio de Pago Kontable (Día 1 de 3 de Gracia)*\n\n"
                f"Hola *{user.full_name}*, tu plan *{subscription.plan}* para *{business_name}* "
                f"ha llegado a su fecha de corte hoy ({cutoff_fmt}).\n\n"
                "🎁 Cuentas con un *periodo de gracia de 72 horas (3 días)* para realizar tu renovación sin interrupción del servicio.\n"
                f"💰 *Total a pagar:* ${price_cop:,.0f} COP\n"
                f"⏳ *Plazo máximo antes de suspensión:* {grace_end_fmt}\n\n"
                "💳 Por favor envía tu soporte de transferencia a la administración comercial para mantener tus consultas DIAN al día."
            )
        elif day_of_grace == 2:
            return (
                "⏳ *Aviso de Pago Kontable (Día 2 de 3 de Gracia)*\n\n"
                f"Hola *{user.full_name}*, te recordamos que restan *2 días* de gracia para tu plan *{subscription.plan}* (*{business_name}*).\n\n"
                f"💰 *Valor pendiente:* ${price_cop:,.0f} COP\n"
                f"📅 *Tu servicio continuará activo hasta el:* {grace_end_fmt}\n\n"
                "Evita la suspensión automática de tus alertas tributarias y reportes fiscales realizando tu pago hoy."
            )
        else:  # Día 3 o último día
            return (
                "🚨 *¡ÚLTIMO DÍA DE GRACIA! Suspensión Inminente de Servicio*\n\n"
                f"Hola *{user.full_name}*, hoy es el *último día* de tu periodo de gracia para *{business_name}*.\n\n"
                f"⚠️ Si tu pago de *${price_cop:,.0f} COP* no se confirma hoy, a partir de mañana el acceso a la plataforma web "
                "y las consultas del bot de Telegram quedarán *suspendidos automáticamente*.\n\n"
                "📲 Comunícate de inmediato con la administradora comercial para registrar tu pago y evitar la interrupción."
            )

    @classmethod
    def has_payment_warning_banner(cls, subscription: Subscription) -> bool:
        """Determina si la aplicación web debe renderizar el banner de advertencia de mora."""
        return subscription.status == "EN_MORA"

    @classmethod
    def get_payment_warning_banner_info(
        cls, subscription: Subscription, check_date: Optional[date] = None
    ) -> Optional[Dict[str, Any]]:
        """Provee la información estructurada para el banner web de advertencia."""
        if not cls.has_payment_warning_banner(subscription):
            return None

        today = check_date or date.today()
        days_left = max(0, (subscription.grace_period_end - today).days)
        severity = "danger" if days_left <= 1 else "warning"

        return {
            "show_banner": True,
            "severity": severity,
            "days_left_in_grace": days_left,
            "grace_period_end": subscription.grace_period_end.strftime("%d/%m/%Y"),
            "amount_due": float(subscription.final_price),
            "plan": subscription.plan,
            "message": (
                f"Tu suscripción se encuentra en periodo de gracia. Restan {days_left} día(s) "
                f"para regularizar tu pago de ${float(subscription.final_price):,.0f} COP antes de la suspensión."
            ),
        }

    @classmethod
    def run_daily_grace_check(
        cls,
        db: Session,
        execution_date: Optional[date] = None,
        telegram_sender: Optional[Callable[[int, str], bool]] = None,
    ) -> Dict[str, Any]:
        """Procesa todas las suscripciones vencidas, actualiza estado a EN_MORA y envía alertas diarias."""
        today = execution_date or date.today()

        # 0. Procesar y bloquear suscripciones con periodo de gracia vencido (> 72h)
        from dian_automation.subscriptions.lockout_service import SubscriptionLockoutService
        lockout_summary = SubscriptionLockoutService.process_expired_grace_lockouts(
            db=db, execution_date=today, telegram_sender=telegram_sender
        )

        # Seleccionar suscripciones que han llegado a su corte y están en periodo de gracia
        # cutoff_date <= today <= grace_period_end
        subscriptions = (
            db.query(Subscription)
            .filter(
                Subscription.status.in_(["ACTIVO", "EN_MORA"]),
                Subscription.cutoff_date <= today,
                Subscription.grace_period_end >= today,
            )
            .all()
        )

        processed_count = len(subscriptions)
        transitioned_to_mora = 0
        notifications_sent = lockout_summary["notifications_sent"]
        details: List[Dict[str, Any]] = []

        for sub in subscriptions:
            user = sub.client
            if not user:
                continue

            # 1. Transición de estado a EN_MORA
            was_active = False
            if sub.status == "ACTIVO":
                sub.status = "EN_MORA"
                transitioned_to_mora += 1
                was_active = True
                logger.warning(
                    f"Suscripción {sub.id} del cliente {user.email} transicionó a EN_MORA (Corte: {sub.cutoff_date})"
                )

            # 2. Calcular día de gracia y días restantes
            days_diff = (today - sub.cutoff_date).days
            day_of_grace = max(1, min(3, days_diff + 1))
            days_remaining = max(0, (sub.grace_period_end - today).days)

            # 3. Control de Idempotencia: No notificar más de una vez por día
            already_notified_today = False
            if sub.last_notified_at and sub.last_notified_at.date() == today:
                already_notified_today = True

            message_sent = False
            if not already_notified_today:
                biz_name = (
                    user.businesses[0].commercial_name
                    if user.businesses
                    else "tu empresa"
                )
                msg = cls.get_grace_notification_message(
                    subscription=sub,
                    user=user,
                    business_name=biz_name,
                    day_of_grace=day_of_grace,
                    days_remaining=days_remaining,
                )

                if user.is_telegram_linked and user.telegram_chat_id:
                    if telegram_sender:
                        try:
                            telegram_sender(user.telegram_chat_id, msg)
                            message_sent = True
                        except Exception as e:
                            logger.error(f"Error enviando telegram a chat {user.telegram_chat_id}: {e}")
                    else:
                        # Modo default/simulación
                        message_sent = True

                    if message_sent:
                        sub.last_notified_at = datetime.combine(today, datetime.now().time())
                        notifications_sent += 1
                        logger.info(
                            f"Notificación de gracia (Día {day_of_grace}) enviada a {user.email} (Chat ID: {user.telegram_chat_id})"
                        )

            details.append({
                "subscription_id": sub.id,
                "client_id": user.id,
                "client_email": user.email,
                "status": sub.status,
                "day_of_grace": day_of_grace,
                "days_remaining": days_remaining,
                "transitioned_now": was_active,
                "notification_sent": message_sent,
                "already_notified_today": already_notified_today,
            })

        db.commit()

        summary = {
            "execution_date": today.isoformat(),
            "processed_count": processed_count,
            "transitioned_to_mora": transitioned_to_mora,
            "locked_out_count": lockout_summary["locked_out_count"],
            "notifications_sent": notifications_sent,
            "details": details,
        }

        logger.info(
            f"Cron de gracia finalizado para {today}: {processed_count} procesadas, "
            f"{transitioned_to_mora} a EN_MORA, {notifications_sent} notificaciones enviadas."
        )

        return summary
