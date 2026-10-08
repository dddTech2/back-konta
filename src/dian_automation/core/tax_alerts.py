"""Alertas proactivas de vencimientos tributarios por Telegram (Story 4.1c).

Recorre los negocios activos que facturan electrónicamente (DIAN) y calcula sus
obligaciones tributarias próximas o que vencen el día de hoy, enviando recordatorios
idempotentes por Telegram a los contribuyentes habilitados.
"""

import logging
import re
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional
from sqlalchemy.orm import Session

from dian_automation.config import config
from dian_automation.core.calendar_engine import (
    CalendarNotLoadedError,
    Obligation,
    TaxCalendarEngine,
    format_limit_date,
    tax_type_label,
    today_bogota,
)
from dian_automation.db.models import (
    Business,
    INCOME_SOURCE_DIAN,
    TaxDeadlineAlert,
    User,
    MOMENT_PROXIMO,
    MOMENT_VENCE_HOY,
    ALERT_STATUS_SENT,
    ALERT_STATUS_SKIPPED,
    ALERT_SKIP_REASON_BLOQUEADO,
    ALERT_SKIP_REASON_SIN_TELEGRAM,
    ALERT_SKIP_REASON_ENVIO_FALLIDO,
)
from dian_automation.subscriptions.lockout_service import SubscriptionLockoutService
from dian_automation.telegram.admin_alerts import notify_tech_ops
from dian_automation.telegram.notify import send_telegram_message

logger = logging.getLogger("tax_alerts")


def _escape_markdown(text: str) -> str:
    """Escapa los caracteres especiales del Markdown legado de Telegram."""
    return re.sub(r"([_*`\[])", r"\\\1", text)


class TaxDeadlineAlerter:
    """Motor de evaluación y envío de recordatorios proactivos de vencimientos tributarios."""

    def __init__(
        self,
        db_session_factory: Callable[[], Session],
        sender: Callable[[int, str], bool] = send_telegram_message,
        upcoming_days: Optional[int] = None,
        tech_ops_notifier: Optional[Callable[[Session, str], Any]] = notify_tech_ops,
    ):
        self.db_session_factory = db_session_factory
        self.sender = sender
        self.upcoming_days = (
            upcoming_days if upcoming_days is not None else config.calendar_upcoming_days
        )
        self.tech_ops_notifier = tech_ops_notifier
        self._last_calendar_missing_reported_date: Optional[date] = None

    def _build_message(self, business: Business, ob: Obligation, moment: str) -> str:
        """Construye el texto del mensaje formateado en Markdown legado de Telegram."""
        biz_name = business.commercial_name or business.legal_name or ""
        escaped_biz = _escape_markdown(biz_name)
        escaped_label = _escape_markdown(ob.etiqueta)
        impuesto = tax_type_label(ob.tax_type)
        fecha = format_limit_date(ob.fecha_limite)

        if moment == MOMENT_PROXIMO:
            dias_text = "en *1 día*" if ob.dias == 1 else f"en *{ob.dias} días*"
            return (
                f"📅 *Recordatorio tributario — {escaped_biz}*\n\n"
                f"Tu *{impuesto}* ({escaped_label}) vence el *{fecha}*, {dias_text}.\n\n"
                "Escribe /vencimientos para ver tu calendario completo."
            )
        elif moment == MOMENT_VENCE_HOY:
            return (
                f"🚨 *¡Hoy vence un impuesto! — {escaped_biz}*\n\n"
                f"Tu *{impuesto}* ({escaped_label}) vence *hoy, {fecha}*.\n\n"
                "Si ya la presentaste, ignora este mensaje."
            )
        raise ValueError(f"Momento de alerta no soportado: {moment}")

    def _process_business_obligations(
        self,
        db: Session,
        business: Business,
        obligations: List[Obligation],
        today: date,
    ) -> tuple[int, int]:
        """Procesa las obligaciones de un negocio, enviando o registrando omisión de alertas."""
        sent_count = 0
        skipped_count = 0

        for ob in obligations:
            if ob.estado != "proximo" or ob.dias is None:
                continue

            if ob.dias == 0:
                moment = MOMENT_VENCE_HOY
            elif 1 <= ob.dias <= self.upcoming_days:
                moment = MOMENT_PROXIMO
            else:
                continue

            # Verificar si ya existe registro para esta obligación y momento
            existing = (
                db.query(TaxDeadlineAlert)
                .filter(
                    TaxDeadlineAlert.business_id == business.id,
                    TaxDeadlineAlert.tax_type == ob.tax_type,
                    TaxDeadlineAlert.period_label == ob.etiqueta,
                    TaxDeadlineAlert.installment == ob.installment,
                    TaxDeadlineAlert.deadline_date == ob.fecha_limite,
                    TaxDeadlineAlert.moment == moment,
                )
                .first()
            )

            # Si ya se envió con éxito previamente para este momento, no se reenvía
            if existing is not None and existing.status == ALERT_STATUS_SENT:
                continue

            # Evaluar elegibilidad del destinatario (dueño del negocio)
            user = business.client
            if user is None:
                user = db.query(User).filter(User.id == business.client_id).first()

            if (
                not user
                or not user.is_active
                or not user.is_telegram_linked
                or not user.telegram_chat_id
            ):
                skip_reason = ALERT_SKIP_REASON_SIN_TELEGRAM
            elif SubscriptionLockoutService.is_client_blocked(db, user.id):
                skip_reason = ALERT_SKIP_REASON_BLOQUEADO
            else:
                skip_reason = None

            if skip_reason is not None:
                if existing is not None:
                    existing.status = ALERT_STATUS_SKIPPED
                    existing.skip_reason = skip_reason
                    existing.sent_at = None
                else:
                    alert = TaxDeadlineAlert(
                        business_id=business.id,
                        tax_type=ob.tax_type,
                        period_label=ob.etiqueta,
                        installment=ob.installment,
                        deadline_date=ob.fecha_limite,
                        moment=moment,
                        status=ALERT_STATUS_SKIPPED,
                        skip_reason=skip_reason,
                        sent_at=None,
                    )
                    db.add(alert)
                skipped_count += 1
                continue

            # Cliente elegible: intentar envío vía Telegram
            msg = self._build_message(business, ob, moment)
            sent_ok = False
            try:
                sent_ok = bool(self.sender(user.telegram_chat_id, msg))
            except Exception:
                logger.exception(
                    f"Error enviando mensaje de Telegram a chat {user.telegram_chat_id} (negocio {business.id})"
                )
                sent_ok = False

            if sent_ok:
                now_utc = datetime.utcnow()
                if existing is not None:
                    existing.status = ALERT_STATUS_SENT
                    existing.skip_reason = None
                    existing.sent_at = now_utc
                else:
                    alert = TaxDeadlineAlert(
                        business_id=business.id,
                        tax_type=ob.tax_type,
                        period_label=ob.etiqueta,
                        installment=ob.installment,
                        deadline_date=ob.fecha_limite,
                        moment=moment,
                        status=ALERT_STATUS_SENT,
                        skip_reason=None,
                        sent_at=now_utc,
                    )
                    db.add(alert)
                sent_count += 1
            else:
                if existing is not None:
                    existing.status = ALERT_STATUS_SKIPPED
                    existing.skip_reason = ALERT_SKIP_REASON_ENVIO_FALLIDO
                    existing.sent_at = None
                else:
                    alert = TaxDeadlineAlert(
                        business_id=business.id,
                        tax_type=ob.tax_type,
                        period_label=ob.etiqueta,
                        installment=ob.installment,
                        deadline_date=ob.fecha_limite,
                        moment=moment,
                        status=ALERT_STATUS_SKIPPED,
                        skip_reason=ALERT_SKIP_REASON_ENVIO_FALLIDO,
                        sent_at=None,
                    )
                    db.add(alert)
                skipped_count += 1

        return sent_count, skipped_count

    def run(self, today: Optional[date] = None) -> Dict[str, Any]:
        """Ejecuta una corrida de alertas de vencimientos tributarios."""
        if today is None:
            today = today_bogota()

        total_sent = 0
        total_skipped = 0
        total_errors = 0
        calendar_missing = False

        db = self.db_session_factory()
        try:
            businesses = (
                db.query(Business)
                .filter(
                    Business.is_active.is_(True),
                    Business.income_source == INCOME_SOURCE_DIAN,
                )
                .all()
            )

            engine = TaxCalendarEngine(db, upcoming_days=self.upcoming_days)
            for business in businesses:
                try:
                    obligations = engine.obligations(business, today)
                except CalendarNotLoadedError:
                    calendar_missing = True
                    if self._last_calendar_missing_reported_date != today:
                        if self.tech_ops_notifier is not None:
                            notice = (
                                f"El calendario tributario de {today.year} no está cargado: "
                                "no se enviaron alertas de vencimientos. "
                                f"Cárgalo con `python -m dian_automation.core.calendar_loader <csv>`."
                            )
                            try:
                                self.tech_ops_notifier(db, notice)
                            except Exception:
                                logger.exception("Error notificando a Soporte TI sobre calendario no cargado.")
                        self._last_calendar_missing_reported_date = today
                    db.rollback()
                    break
                except Exception:
                    db.rollback()
                    total_errors += 1
                    logger.exception(f"Error calculando obligaciones para el negocio {business.id}")
                    continue

                try:
                    b_sent, b_skipped = self._process_business_obligations(
                        db, business, obligations, today
                    )
                    total_sent += b_sent
                    total_skipped += b_skipped
                    db.commit()
                except Exception:
                    db.rollback()
                    total_errors += 1
                    logger.exception(f"Error procesando alertas para el negocio {business.id}")
        finally:
            db.close()

        return {
            "sent": total_sent,
            "skipped": total_skipped,
            "errors": total_errors,
            "calendar_missing": calendar_missing,
        }
