"""Bot de Telegram para Contribuyentes (Clientes).

Permite a los clientes consultar en tiempo real desde Telegram:
- /resumen: Facturación mensual, variación porcentual, IVA generado/descontable y balance a pagar o a favor.
- /facturas: Últimas 4 facturas electrónicas emitidas.
- /vencimientos: Calendario de obligaciones tributarias DIAN según el último dígito del NIT.
- /registrar_venta: Registra una venta (total y descripción opcional) vía core/sales_service.py.
- /mis_ventas: Lista las últimas ventas registradas del negocio con numeración para anulación.
- /anular_venta: Anula una venta mal registrada según su posición en la lista reciente.
- /ayuda: Menú interactivo de comandos disponibles.

Incluye control de acceso mediante verificación de vinculación y estado de suscripción.
"""

import os
import re
import logging
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, Dict, Any, Tuple, List
from zoneinfo import ZoneInfo
import httpx
from sqlalchemy.orm import Session

from dian_automation.branding import BRAND_NAME, BRAND_TAGLINE
from dian_automation.config import config
from dian_automation.core import income_service, sales_service
from dian_automation.db.models import (
    User,
    Business,
    MonthlyTaxSummary,
    Invoice,
    INCOME_SOURCE_MANUAL_SALES,
)
from dian_automation.core.calendar_engine import (
    CalendarNotLoadedError,
    TaxCalendarEngine,
    today_bogota,
)
from dian_automation.subscriptions.lockout_service import SubscriptionLockoutService

logger = logging.getLogger("client_bot")

# httpx loguea a nivel INFO la URL completa de cada petición y la de Telegram lleva el token del bot.
logging.getLogger("httpx").setLevel(logging.WARNING)


def _format_cop(amount: Decimal) -> str:
    """Formatea monto en COP: $1,500,000 sin decimales si es entero, con 2 si no."""
    is_integer = (amount % 1 == 0)
    abs_amt = abs(amount)
    fmt = f"${abs_amt:,.0f}" if is_integer else f"${abs_amt:,.2f}"
    if amount < 0:
        return f"-{fmt} COP"
    return f"{fmt} COP"


class ClientTelegramBot:
    """Procesador de consultas tributarias y facturación para contribuyentes."""

    @classmethod
    def get_authenticated_client(
        cls, sender_chat_id: int, db: Session
    ) -> Tuple[Optional[User], Optional[Business], Optional[str], Optional[str]]:
        """Valida que el chat_id corresponda a un cliente activo, vinculado y sin bloqueo de suscripción.
        
        Returns:
            Tuple (User, Business, error_code, error_message)
        """
        user = (
            db.query(User)
            .filter(
                User.telegram_chat_id == sender_chat_id,
                User.is_telegram_linked.is_(True),
                User.is_active.is_(True),
            )
            .first()
        )

        if not user:
            return (
                None,
                None,
                "UNLINKED",
                "⛔ *Cuenta no vinculada*\n\n"
                f"No encontramos ninguna cuenta de {BRAND_NAME} asociada a este chat de Telegram.\n\n"
                "📲 Si acabas de adquirir tu plan, pulsa en el enlace de invitación de un solo uso "
                "que te envió tu administradora comercial para activar tu acceso.",
            )

        # Verificar bloqueo de suscripción (Epic 3 hook)
        if SubscriptionLockoutService.is_client_blocked(db, user.id):
            return (
                user,
                None,
                "SUBSCRIPTION_BLOCKED",
                "⚠️ *Servicio Suspendido*\n\n"
                "Tu suscripción se encuentra suspendida temporalmente por pago pendiente. "
                "Comunícate con Katerinn para reactivar tus reportes.",
            )

        # Obtener el negocio principal asociado al cliente
        business = (
            db.query(Business)
            .filter(Business.client_id == user.id, Business.is_active.is_(True))
            .first()
        )

        if not business:
            return (
                user,
                None,
                "NO_BUSINESS",
                "⚠️ *Sin empresa registrada*\n\n"
                "Tu usuario está activo pero aún no tiene una empresa o negocio asociado. "
                "Por favor comunícate con soporte.",
            )

        return user, business, None, None

    @classmethod
    def handle_resumen(
        cls, sender_chat_id: int, db: Session, target_period: Optional[str] = None
    ) -> Dict[str, Any]:
        """Procesa el comando /resumen: total facturado, variación %, IVA generado, descontable y saldo neto."""
        user, business, err_code, err_msg = cls.get_authenticated_client(sender_chat_id, db)
        if err_code:
            return {"success": False, "reason": err_code, "message": err_msg}

        if business.income_source == INCOME_SOURCE_MANUAL_SALES:
            month = target_period or datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m")
            try:
                data = income_service.summary(db, business, month)
            except income_service.IncomeError:
                return {
                    "success": False,
                    "reason": income_service.IncomeError.INVALID_MONTH,
                    "message": "❌ Mes inválido. Usa el formato `/resumen AAAA-MM`, por ejemplo `/resumen 2026-08`.",
                }
            ingresos = data["ingresos"]
            egresos = data["egresos"]
            utilidad = data["utilidad"]

            has_data = not (ingresos == Decimal("0.00") and egresos == Decimal("0.00") and utilidad == Decimal("0.00"))

            message = (
                f"📊 *Resumen del mes - {business.commercial_name}*\n"
                f"🏢 NIT: `{business.nit}-{business.dv}`\n"
                f"📅 *Mes:* {month}\n"
                "───────────────────────────────\n"
                f"💵 *Ingresos:* {_format_cop(ingresos)}\n"
                f"💸 *Egresos:* {_format_cop(egresos)}\n"
                f"📈 *Utilidad estimada:* {_format_cop(utilidad)}\n"
                "───────────────────────────────\n"
                "ℹ️ _Los egresos corresponden al total de tus facturas recibidas._"
            )

            return {
                "success": True,
                "has_data": has_data,
                "period": month,
                "ingresos": ingresos,
                "egresos": egresos,
                "utilidad": utilidad,
                "message": message,
            }

        query = db.query(MonthlyTaxSummary).filter(MonthlyTaxSummary.business_id == business.id)
        if target_period:
            summary = query.filter(MonthlyTaxSummary.period_year_month == target_period).first()
        else:
            summary = query.order_by(MonthlyTaxSummary.period_year_month.desc()).first()

        if not summary:
            return {
                "success": True,
                "has_data": False,
                "message": (
                    f"📊 *Resumen Fiscal - {business.commercial_name}*\n"
                    f"🏢 NIT: `{business.nit}-{business.dv}`\n\n"
                    "ℹ️ Aún no dispones de resúmenes fiscales liquidados en la plataforma.\n"
                    "Tan pronto como la DIAN sincronice los documentos electrónicos del mes, "
                    "podrás consultar tus cifras actualizadas aquí."
                ),
            }

        # Formatear balance de IVA (A pagar vs A favor)
        iva_bal = float(summary.iva_balance)
        if iva_bal > 0:
            balance_str = f"🔴 *Saldo a Pagar DIAN:* ${iva_bal:,.0f} COP"
        elif iva_bal < 0:
            balance_str = f"🟢 *Saldo a Favor:* ${abs(iva_bal):,.0f} COP"
        else:
            balance_str = "⚪ *Saldo Neto de IVA:* $0 COP"

        # Variación % vs mes anterior
        if summary.variation_vs_previous_pct is not None:
            var_val = float(summary.variation_vs_previous_pct)
            var_arrow = "📈 +" if var_val > 0 else ("📉 " if var_val < 0 else "➡️ ")
            var_str = f"{var_arrow}{var_val:.1f}%"
        else:
            var_str = "N/D (Periodo inicial)"

        message = (
            f"📊 *RESUMEN FISCAL DEL MES ({summary.period_year_month})*\n"
            f"🏢 *Empresa:* {business.commercial_name} (`NIT {business.nit}-{business.dv}`)\n"
            "───────────────────────────────\n"
            f"💵 *Total Facturado:* ${float(summary.total_invoiced_net):,.0f} COP\n"
            f"📊 *Variación vs Mes Anterior:* {var_str}\n"
            f"📑 *Documentos Procesados:* {summary.total_invoices_count}\n"
            "───────────────────────────────\n"
            "🏛️ *LIQUIDACIÓN DE IVA:*\n"
            f"  • *IVA Generado (Ventas):* ${float(summary.iva_generado):,.0f} COP\n"
            f"  • *IVA Descontable (Compras):* ${float(summary.iva_descontable):,.0f} COP\n"
            f"  • {balance_str}\n"
            "───────────────────────────────\n"
            "⚖️ *RETENCIONES EN LA FUENTE:*\n"
            f"  • *ReteIVA:* ${float(summary.rete_iva_total):,.0f} COP\n"
            f"  • *ReteRenta:* ${float(summary.rete_renta_total):,.0f} COP\n"
            f"  • *ReteICA:* ${float(summary.rete_ica_total):,.0f} COP\n\n"
            f"🕒 _Calculado automáticamente el {summary.calculated_at.strftime('%d/%m/%Y %H:%M')}_"
        )

        return {
            "success": True,
            "has_data": True,
            "period": summary.period_year_month,
            "total_invoiced": float(summary.total_invoiced_net),
            "iva_balance": iva_bal,
            "message": message,
        }

    @classmethod
    def handle_facturas(
        cls, sender_chat_id: int, db: Session, limit: int = 4
    ) -> Dict[str, Any]:
        """Procesa el comando /facturas: devuelve las últimas 4 facturas electrónicas emitidas."""
        user, business, err_code, err_msg = cls.get_authenticated_client(sender_chat_id, db)
        if err_code:
            return {"success": False, "reason": err_code, "message": err_msg}

        if business.income_source == INCOME_SOURCE_MANUAL_SALES:
            return {
                "success": True,
                "count": 0,
                "message": "ℹ️ /facturas lista las facturas electrónicas emitidas y no aplica a tu tipo de negocio. Usa /resumen para ver tus ingresos y egresos, o /mis_ventas para tus ventas.",
            }

        invoices = (
            db.query(Invoice)
            .filter(
                Invoice.business_id == business.id,
                Invoice.group_type == "Emitido",
            )
            .order_by(Invoice.issue_date.desc())
            .limit(limit)
            .all()
        )

        if not invoices:
            return {
                "success": True,
                "count": 0,
                "message": (
                    f"🧾 *Últimas Facturas Emitidas - {business.commercial_name}*\n"
                    f"🏢 NIT: `{business.nit}-{business.dv}`\n\n"
                    "ℹ️ No se registran facturas electrónicas emitidas en tu historial reciente."
                ),
            }

        lines = [
            f"🧾 *ÚLTIMAS FACTURAS EMITIDAS ({len(invoices)})*",
            f"🏢 *{business.commercial_name}* (NIT `{business.nit}-{business.dv}`)",
            "───────────────────────────────",
        ]

        for i, inv in enumerate(invoices, 1):
            inv_code = f"{inv.prefix or ''}-{inv.folio or ''}".strip("-")
            date_str = inv.issue_date.strftime("%d/%m/%Y")
            total_str = f"${float(inv.total):,.0f} COP"
            client_name = inv.receiver_name[:30] + "..." if len(inv.receiver_name) > 30 else inv.receiver_name

            lines.append(f"{i}️⃣ *Factura:* `{inv_code}`")
            lines.append(f"   👤 *Cliente:* {client_name}")
            lines.append(f"   📅 *Fecha:* {date_str} | 💰 *Total:* {total_str}")
            if inv.dian_status:
                lines.append(f"   🏛️ *Estado DIAN:* {inv.dian_status}")
            lines.append("")

        lines.append("ℹ️ _Usa /resumen para ver el balance mensual consolidado de impuestos._")

        return {
            "success": True,
            "count": len(invoices),
            "message": "\n".join(lines).strip(),
        }

    @classmethod
    def handle_vencimientos(
        cls, sender_chat_id: int, db: Session, reference_date: Optional[date] = None
    ) -> Dict[str, Any]:
        """Procesa el comando /vencimientos: lista las obligaciones tributarias según el último dígito del NIT."""
        user, business, err_code, err_msg = cls.get_authenticated_client(sender_chat_id, db)
        if err_code:
            return {"success": False, "reason": err_code, "message": err_msg}

        if business.income_source == INCOME_SOURCE_MANUAL_SALES:
            return {
                "success": True,
                "count": 0,
                "message": (
                    f"🗓️ *Calendario Tributario DIAN*\n\n"
                    "ℹ️ El calendario de vencimientos tributarios no aplica a tu tipo de negocio."
                ),
            }

        # Extraer el último dígito del NIT (sin el DV)
        clean_nit = "".join(filter(str.isdigit, business.nit))
        if not clean_nit:
            last_digit = 0
        else:
            last_digit = int(clean_nit[-1])

        ref_date = reference_date or today_bogota()

        try:
            obligations = TaxCalendarEngine(db).obligations(business, ref_date)
        except CalendarNotLoadedError:
            return {
                "success": False,
                "reason": "CALENDAR_NOT_LOADED",
                "message": (
                    "🗓️ *Calendario Tributario DIAN*\n\n"
                    "ℹ️ El calendario tributario de este año aún no está cargado. Inténtalo de nuevo más tarde."
                ),
            }

        if not obligations:
            return {
                "success": True,
                "count": 0,
                "message": (
                    f"🗓️ *Calendario Tributario DIAN*\n"
                    f"🏢 *{business.commercial_name}* (NIT `{business.nit}-{business.dv}`, Dígito: `{last_digit}`)\n\n"
                    "ℹ️ No hay obligaciones tributarias registradas actualmente para tu terminación de NIT."
                ),
            }

        lines = [
            "🗓️ *CALENDARIO DE VENCIMIENTOS DIAN*",
            f"🏢 *{business.commercial_name}*",
            f"🆔 *NIT:* `{business.nit}-{business.dv}` (Último dígito: *{last_digit}*)",
            "───────────────────────────────",
        ]

        # Filtrar o clasificar próximas obligaciones
        for ob in obligations:
            days_diff = (ob.fecha_limite - ref_date).days
            if days_diff < 0:
                status_tag = f"⚠️ Venció hace {abs(days_diff)} días"
                icon = "🔴"
            elif days_diff == 0:
                status_tag = "🚨 ¡VENCE HOY!"
                icon = "🔥"
            elif days_diff <= 5:
                status_tag = f"⏳ En {days_diff} días"
                icon = "🟡"
            else:
                status_tag = f"📅 En {days_diff} días"
                icon = "🟢"

            lines.append(f"{icon} *{ob.tax_type}* ({ob.etiqueta})")
            lines.append(f"   Fecha Límite: *{ob.fecha_limite.strftime('%d/%m/%Y')}* — _{status_tag}_")
            if ob.description:
                lines.append(f"   _{ob.description}_")
            lines.append("")

        lines.append(f"💡 *Tip {BRAND_NAME}:* Presenta y paga con anticipación para evitar sanciones e intereses de mora.")

        return {
            "success": True,
            "count": len(obligations),
            "last_digit": last_digit,
            "message": "\n".join(lines).strip(),
        }

    @classmethod
    def handle_dashboard(cls, sender_chat_id: int, db: Session) -> Dict[str, Any]:
        """Procesa el comando /dashboard: entrega el enlace al prototipo web/móvil con datos reales."""
        user, business, err_code, err_msg = cls.get_authenticated_client(sender_chat_id, db)
        if err_code:
            return {"success": False, "reason": err_code, "message": err_msg}

        link = f"{config.kontable_web_url}?nit={business.nit}"
        message = (
            f"📱 *Tu Dashboard {BRAND_NAME} — {business.commercial_name}*\n\n"
            f"{link}\n\n"
            "_Inicia sesión con tu celular o el NIT de tu negocio: te enviaremos un código por este chat._"
        )
        return {"success": True, "link": link, "message": message}

    @staticmethod
    def send_otp(chat_id: int, code: str) -> bool:
        """Envía el código OTP de login web al chat vinculado; True si Telegram lo aceptó.

        No registra el texto ni la URL (contiene el token del bot) ni el código: ante un fallo
        solo se loguea el tipo de error.
        """
        token = (
            os.getenv("TELEGRAM_BOT_TOKEN")
            or os.getenv("TELEGRAM_ADMIN_BOT_TOKEN")
            or os.getenv("TELEGRAM_CLIENT_BOT_TOKEN")
        )
        if not token:
            logger.error("TELEGRAM_BOT_TOKEN no configurado: no se puede enviar el código OTP.")
            return False

        text = (
            f"🔐 Tu código de acceso a {BRAND_NAME} es: *{code}*\n"
            "Vence en 5 minutos y solo sirve una vez. No lo compartas con nadie."
        )
        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=3.0,
            )
            return response.status_code == 200 and bool(response.json().get("ok"))
        except Exception as exc:  # noqa: BLE001 - red, timeout o respuesta no JSON
            logger.error("Fallo al enviar el código OTP por Telegram (%s).", type(exc).__name__)
            return False

    REGISTRAR_VENTA_HELP = (
        "📝 *Registrar venta*\n\n"
        "Formato: `/registrar_venta <total> | <descripción opcional>`\n"
        "Ejemplo: `/registrar_venta 150000 | 3 tortas de chocolate`\n\n"
        "El total va sin `$`, sin comas ni puntos de miles y con máximo 2 decimales (punto decimal)."
    )

    @staticmethod
    def parse_registrar_venta_command(text: str) -> Tuple[str, Optional[str]]:
        """Separa `/registrar_venta <total> [| <descripción>]` en (total crudo, descripción cruda).

        El primer `|` separa total y descripción; todo lo posterior es descripción. Solo sintaxis:
        la validación de ambos valores vive en core/sales_service.py.
        """
        body = re.sub(r"^\s*/registrar_venta(?:@\w+)?", "", text.strip(), flags=re.IGNORECASE)
        raw_total, separator, description = body.partition("|")
        return raw_total.strip(), (description if separator else None)

    @staticmethod
    def _escape_markdown(text: str) -> str:
        """Escapa los caracteres especiales del Markdown legado de Telegram en texto del usuario."""
        return re.sub(r"([_*`\[])", r"\\\1", text)

    @classmethod
    def handle_registrar_venta(cls, sender_chat_id: int, db: Session, text: str) -> Dict[str, Any]:
        """Procesa /registrar_venta: guardia de cliente, luego servicio compartido de ventas."""
        user, business, err_code, err_msg = cls.get_authenticated_client(sender_chat_id, db)
        if err_code:
            return {"success": False, "reason": err_code, "message": err_msg}

        raw_total, raw_description = cls.parse_registrar_venta_command(text)

        try:
            sale = sales_service.register_sale(
                db,
                user=user,
                business=business,
                total=raw_total,
                description=raw_description,
                recorded_via=sales_service.RECORDED_VIA_TELEGRAM,
            )
        except sales_service.SalesError as exc:
            if exc.code == sales_service.SalesError.BUSINESS_BLOCKED:
                message = SubscriptionLockoutService.BLOCKED_TELEGRAM_MESSAGE
            elif exc.code == sales_service.SalesError.NOT_MANUAL_SALES:
                message = f"ℹ️ {exc.message}"
            elif exc.code in (
                sales_service.SalesError.NON_POSITIVE_TOTAL,
                sales_service.SalesError.DESCRIPTION_TOO_LONG,
            ):
                message = f"❌ {exc.message}\n\n{cls.REGISTRAR_VENTA_HELP}"
            else:
                message = cls.REGISTRAR_VENTA_HELP
            return {"success": False, "reason": exc.code, "message": message}

        amount = sale.total_amount
        amount_str = f"${amount:,.2f}" if amount % 1 else f"${amount:,.0f}"
        message = f"✅ Venta registrada: {amount_str} COP"
        if sale.description:
            message += f" — {cls._escape_markdown(sale.description)}"
        return {"success": True, "sale_id": sale.id, "message": message}

    @classmethod
    def handle_mis_ventas(cls, sender_chat_id: int, db: Session) -> Dict[str, Any]:
        """Procesa /mis_ventas: lista hasta 10 ventas recientes no anuladas del negocio."""
        user, business, err_code, err_msg = cls.get_authenticated_client(sender_chat_id, db)
        if err_code:
            return {"success": False, "reason": err_code, "message": err_msg}

        if business.income_source != INCOME_SOURCE_MANUAL_SALES:
            return {
                "success": False,
                "reason": "NOT_MANUAL_SALES",
                "message": "ℹ️ Tu negocio factura electrónicamente: las ventas salen de la DIAN y no se registran a mano.",
            }

        numbered_sales = sales_service.list_recent_sales_numbered(db, business, 10)
        if not numbered_sales:
            return {
                "success": True,
                "count": 0,
                "message": "ℹ️ Aún no tienes ventas registradas.",
            }

        lines = ["🧾 *Tus últimas ventas*", ""]
        for num, sale in numbered_sales:
            date_str = sale.sale_date.strftime("%d/%m/%Y")
            amount_str = _format_cop(sale.total_amount)
            if sale.description:
                desc = cls._escape_markdown(sale.description)
                lines.append(f"{num}. {date_str} — {amount_str} — {desc}")
            else:
                lines.append(f"{num}. {date_str} — {amount_str}")

        first_num = numbered_sales[0][0]
        lines.append("")
        lines.append(f"Para anular una: `/anular_venta {first_num}` (el número de cada venta no cambia).")

        return {
            "success": True,
            "count": len(numbered_sales),
            "message": "\n".join(lines),
        }

    @staticmethod
    def parse_anular_venta_command(text: str) -> Optional[int]:
        """Quita /anular_venta (con @bot opcional) y devuelve el entero positivo o None."""
        if not re.match(r"^\s*/anular_venta(?:@\w+)?(?:\s|$)", text.strip(), flags=re.IGNORECASE):
            return None
        body = re.sub(r"^\s*/anular_venta(?:@\w+)?", "", text.strip(), flags=re.IGNORECASE)
        cleaned = body.strip()
        if not cleaned or not cleaned.isdigit():
            return None
        val = int(cleaned)
        return val if val > 0 else None

    @classmethod
    def handle_anular_venta(
        cls, sender_chat_id: int, db: Session, text: str
    ) -> Dict[str, Any]:
        """Procesa /anular_venta N: anula la venta según su número estable."""
        user, business, err_code, err_msg = cls.get_authenticated_client(sender_chat_id, db)
        if err_code:
            return {"success": False, "reason": err_code, "message": err_msg}

        if business.income_source != INCOME_SOURCE_MANUAL_SALES:
            return {
                "success": False,
                "reason": "NOT_MANUAL_SALES",
                "message": "ℹ️ Tu negocio factura electrónicamente: las ventas salen de la DIAN y no se registran a mano.",
            }

        num = cls.parse_anular_venta_command(text)
        if num is None:
            return {
                "success": False,
                "reason": "INVALID_NUMBER",
                "message": (
                    "❌ Indica el número de la venta a anular, por ejemplo `/anular_venta 2`. "
                    "Mira tu lista con /mis_ventas."
                ),
            }

        sale = sales_service.find_recent_sale_by_number(db, business, num, 10)
        if sale is None:
            return {
                "success": False,
                "reason": "NUMBER_OUT_OF_RANGE",
                "message": "❌ Ese número no está en tu lista actual (o la venta ya está anulada). Usa /mis_ventas para ver las ventas que puedes anular.",
            }

        try:
            voided = sales_service.void_sale(
                db, user=user, business=business, sale_id=sale.id
            )
        except sales_service.SalesError as exc:
            if exc.code == sales_service.SalesError.BUSINESS_BLOCKED:
                message = SubscriptionLockoutService.BLOCKED_TELEGRAM_MESSAGE
            elif exc.code == sales_service.SalesError.NOT_MANUAL_SALES:
                message = f"ℹ️ {exc.message}"
            elif exc.code in (
                sales_service.SalesError.SALE_NOT_FOUND,
                sales_service.SalesError.ALREADY_VOIDED,
            ):
                message = f"❌ {exc.message} Usa /mis_ventas para ver tu lista actual."
            else:
                message = f"❌ {exc.message}"
            return {"success": False, "reason": exc.code, "message": message}

        date_str = voided.sale_date.strftime("%d/%m/%Y")
        amount_str = _format_cop(voided.total_amount)
        if voided.description:
            desc_escaped = cls._escape_markdown(voided.description)
            msg = f"🗑️ Venta anulada: {amount_str} — {desc_escaped} ({date_str})"
        else:
            msg = f"🗑️ Venta anulada: {amount_str} ({date_str})"

        return {
            "success": True,
            "sale_id": voided.id,
            "message": msg,
        }

    @classmethod
    def handle_help(cls, sender_chat_id: int, db: Session) -> str:
        """Devuelve el menú de ayuda y bienvenida para el cliente."""
        user, business, err_code, err_msg = cls.get_authenticated_client(sender_chat_id, db)
        if err_code and err_code != "NO_BUSINESS":
            return err_msg

        biz_name = business.commercial_name if business else "tu empresa"

        if business and business.income_source == INCOME_SOURCE_MANUAL_SALES:
            return (
                f"👋 *¡Hola, bienvenido a {BRAND_NAME} Bot!*\n"
                f"_{BRAND_TAGLINE}_\n"
                f"Asistente contable inteligente para *{biz_name}*.\n\n"
                "Puedes consultar tu información en cualquier momento con estos comandos:\n\n"
                "📊 */resumen* — Ingresos, egresos y utilidad del mes.\n"
                "📱 */dashboard* — Enlace a tu panel web/móvil con gráficos e historial completo.\n"
                "📝 */registrar_venta* — Registra una venta: `/registrar_venta 150000 | descripción opcional`.\n"
                "🧾 */mis_ventas* — Tus últimas 10 ventas, cada una con su número para anular.\n"
                "🗑️ */anular_venta* — Anula una venta mal registrada: `/anular_venta 2`.\n"
                "ℹ️ */ayuda* — Muestra este menú de opciones.\n\n"
                "🔒 _Tus cifras de ingresos se basan en las ventas registradas por el cliente y los egresos en tus facturas electrónicas recibidas._"
            )

        return (
            f"👋 *¡Hola, bienvenido a {BRAND_NAME} Bot!*\n"
            f"_{BRAND_TAGLINE}_\n"
            f"Asistente tributario inteligente para *{biz_name}*.\n\n"
            "Puedes consultar tu información fiscal en cualquier momento con estos comandos:\n\n"
            "📊 */resumen* — Facturación mensual, IVA generado/descontable y saldo a pagar o a favor.\n"
            "🧾 */facturas* — Últimas 4 facturas electrónicas emitidas con clientes y montos.\n"
            "🗓️ */vencimientos* — Fechas límite de tus obligaciones tributarias según tu NIT.\n"
            "📱 */dashboard* — Enlace a tu panel web/móvil con gráficos e historial completo.\n"
            "ℹ️ */ayuda* — Muestra este menú de opciones.\n\n"
            "🔒 _Tus datos provienen directamente del repositorio oficial de la DIAN._"
        )

    @classmethod
    def handle_client_message(
        cls, sender_chat_id: int, text: str, db: Session
    ) -> str:
        """Enrutador de mensajes recibidos en Telegram desde clientes contribuyentes."""
        user, business, err_code, err_msg = cls.get_authenticated_client(sender_chat_id, db)
        if err_code == "SUBSCRIPTION_BLOCKED":
            return err_msg

        text_clean = text.strip().lower()

        if text_clean.startswith("/resumen"):
            # Permite consultar un periodo específico si se envía como /resumen 2026-08
            parts = text.strip().split()
            target_period = parts[1] if len(parts) > 1 and re.match(r"^\d{4}-\d{2}$", parts[1]) else None
            res = cls.handle_resumen(sender_chat_id, db, target_period=target_period)
            return res["message"]

        elif text_clean.startswith("/facturas"):
            res = cls.handle_facturas(sender_chat_id, db, limit=4)
            return res["message"]

        elif text_clean.startswith("/vencimientos"):
            res = cls.handle_vencimientos(sender_chat_id, db)
            return res["message"]

        elif text_clean.startswith("/dashboard"):
            res = cls.handle_dashboard(sender_chat_id, db)
            return res["message"]

        elif re.match(r"^/registrar_venta(?:@\w+)?(?:\s|$)", text_clean):
            res = cls.handle_registrar_venta(sender_chat_id, db, text)
            return res["message"]

        elif re.match(r"^/mis_ventas(?:@\w+)?(?:\s|$)", text_clean):
            res = cls.handle_mis_ventas(sender_chat_id, db)
            return res["message"]

        elif re.match(r"^/anular_venta(?:@\w+)?(?:\s|$)", text_clean):
            res = cls.handle_anular_venta(sender_chat_id, db, text)
            return res["message"]

        elif text_clean.startswith("/ayuda") or text_clean.startswith("/help") or text_clean == "/start":
            return cls.handle_help(sender_chat_id, db)

        if business and business.income_source == INCOME_SOURCE_MANUAL_SALES:
            return (
                "🤖 No reconozco ese comando.\n\n"
                "Comandos disponibles:\n"
                "• /resumen — Ingresos, egresos y utilidad del mes\n"
                "• /dashboard — Enlace a tu panel web/móvil\n"
                "• /registrar_venta — Registra una venta (total y descripción opcional)\n"
                "• /mis_ventas — Tus últimas 10 ventas\n"
                "• /anular_venta — Anula una venta mal registrada\n"
                "• /ayuda — Menú de ayuda"
            )

        return (
            "🤖 No reconozco ese comando.\n\n"
            "Comandos disponibles:\n"
            "• /resumen — Resumen fiscal del mes e IVA\n"
            "• /facturas — Últimas 4 facturas emitidas\n"
            "• /vencimientos — Calendario de impuestos DIAN\n"
            "• /dashboard — Enlace a tu panel web/móvil\n"
            "• /ayuda — Menú de ayuda"
        )
