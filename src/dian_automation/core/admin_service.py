"""Servicio de dominio para operaciones de administración comercial.

Compartido entre el bot de Telegram y el panel web de administración.
No conoce chat_id, Markdown ni Telegram (desacoplado de la capa de presentación).
"""

import calendar
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from dian_automation.branding import BRAND_NAME
from dian_automation.config import config
from dian_automation.db.models import (
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    IVA_PERIODICITY_BIMESTRAL,
    IVA_PERIODICITY_CUATRIMESTRAL,
    Business,
    BusinessDocument,
    DIANExtractionJob,
    PaymentRecord,
    PricingPlan,
    Subscription,
    SubscriptionPriceChange,
    TelegramLinkToken,
    User,
    WorkerHeartbeat,
    PRICE_ORIGIN_TARIFA,
)
from dian_automation.queue.exceptions import is_slow_error
from dian_automation.subscriptions.pricing import PricingService, PricingError
from dian_automation.subscriptions.service import add_months_to_date
from dian_automation.telegram.admin_alerts import _cause_text
from dian_automation.telegram.deep_linking import TelegramDeepLinkingService

logger = logging.getLogger("admin_service")

# Tabla paramétrica de multiplicadores DIAN para cálculo de dígito de verificación
DIAN_DV_WEIGHTS = [71, 67, 59, 53, 47, 43, 41, 37, 29, 23, 19, 17, 13, 7, 3]

INCOME_SOURCE_BY_TIPO: Dict[str, str] = {
    "FACTURADOR": INCOME_SOURCE_DIAN,
    "VENTAS_MANUALES": INCOME_SOURCE_MANUAL_SALES,
}
INCOME_SOURCE_LABELS: Dict[str, str] = {
    INCOME_SOURCE_DIAN: "🧾 Facturador electrónico (cifras desde la DIAN)",
    INCOME_SOURCE_MANUAL_SALES: "✍️ Ventas manuales (registra sus ventas a mano)",
}


class AdminServiceError(Exception):
    """Excepción de dominio para operaciones de administración con código tipado y status HTTP."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        extra: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}


@dataclass
class NewClientData:
    """Datos de entrada tipados para alta o renovación de cliente."""
    tipo_cliente: str  # "PERSONA" o "EMPRESA"
    full_name: str
    phone: str
    nit: str
    plan: str
    business_name: Optional[str] = None
    legal_rep_doc: Optional[str] = None
    income_source: Optional[str] = None


@dataclass
class CreatedClient:
    """Resultado tipado de la creación de cliente."""
    user: User
    business: Business
    subscription: Subscription
    deep_link_url: str
    tipo_cliente: str
    nit_with_dv: str
    final_price: float
    discount_rate: float
    cutoff_date: date
    grace_period_end: date
    monthly_price: Optional[Decimal] = None


@dataclass
class PaymentResult:
    """Resultado tipado de la confirmación de pago y reactivación."""
    payment: PaymentRecord
    subscription: Subscription
    client: User
    business: Business
    new_cutoff_date: date
    client_notified: bool
    expected_amount: Decimal
    difference: Decimal


def calculate_dian_dv(nit: str) -> str:
    """Calcula el dígito de verificación oficial de la DIAN mediante algoritmo módulo 11."""
    nit_clean = "".join(filter(str.isdigit, str(nit)))
    if not nit_clean:
        return "0"

    nit_reversed = nit_clean[::-1]
    total = 0
    for i, digit_char in enumerate(nit_reversed):
        weight = DIAN_DV_WEIGHTS[-(i + 1)] if (i + 1) <= len(DIAN_DV_WEIGHTS) else 3
        total += int(digit_char) * weight

    residue = total % 11
    if residue <= 1:
        return str(residue)
    return str(11 - residue)


def normalize_nit(raw: Optional[str]) -> Optional[str]:
    """NIT solo con dígitos; admite `-DV` final (901008579-7). None si tiene menos de 6 dígitos."""
    if not raw:
        return None
    raw_clean = raw.strip()
    if "-" in raw_clean:
        possible_dv = raw_clean.rsplit("-", 1)[-1].strip()
        if len(possible_dv) == 1 and possible_dv.isdigit():
            raw_clean = raw_clean.rsplit("-", 1)[0]
    nit = "".join(filter(str.isdigit, raw_clean))
    return nit if len(nit) >= 6 else None


def resolve_months_range(n_months: int, reference_date: Optional[date] = None) -> str:
    """Calcula un único rango cubriendo los últimos N meses calendario COMPLETOS.

    El mes en curso nunca cuenta (no está completo).
    """
    today = reference_date or date.today()

    end_year, end_month = today.year, today.month - 1
    if end_month == 0:
        end_month, end_year = 12, end_year - 1
    end_last_day = calendar.monthrange(end_year, end_month)[1]
    end_date = date(end_year, end_month, end_last_day)

    start_of_end_month = date(end_year, end_month, 1)
    start_date = add_months_to_date(start_of_end_month, -(n_months - 1))

    return f"{start_date.isoformat()} - {end_date.isoformat()}"


def create_client(
    db: Session,
    admin: Optional[User],
    data: NewClientData,
    bot_username: str = "KontaBot",
    allow_existing_nit: bool = True,
) -> CreatedClient:
    """Crea o reactiva un cliente (persona o empresa), su negocio y suscripción, generando enlace mágico."""
    plan_clean = (data.plan or "").upper().strip()
    try:
        plan_obj = PricingService.get_plan(db, plan_clean, active_only=True)
    except PricingError as pe:
        active_plans = [p.code for p in PricingService.list_plans(db, active_only=True)]
        valid_plans = ", ".join(active_plans)
        raise AdminServiceError("INVALID_PLAN", f"Plan '{data.plan}' no reconocido. Opciones válidas: {valid_plans}", 400) from pe

    tipo_clean = (data.tipo_cliente or "").upper().strip()
    if tipo_clean not in ("PERSONA", "EMPRESA"):
        raise AdminServiceError("INVALID_CLIENT_TYPE", f"Tipo de cliente '{data.tipo_cliente}' no reconocido. Debe ser PERSONA o EMPRESA.", 400)

    nit_clean = normalize_nit(data.nit)
    if not nit_clean:
        campo = "NIT de la empresa" if tipo_clean == "EMPRESA" else "Cédula"
        raise AdminServiceError("INVALID_NIT", f"El {campo} '{data.nit}' no es válido (debe tener al menos 6 dígitos numéricos).", 400)

    legal_rep_clean = None
    if tipo_clean == "EMPRESA":
        if not data.business_name or not data.business_name.strip():
            raise AdminServiceError("MISSING_BUSINESS_NAME", "El nombre de la empresa no puede estar vacío.", 400)
        if not data.legal_rep_doc or not data.legal_rep_doc.strip():
            raise AdminServiceError("MISSING_LEGAL_REP", "La Cédula del Representante Legal es obligatoria para empresas.", 400)
        legal_rep_clean = "".join(filter(str.isdigit, data.legal_rep_doc.strip()))
        if len(legal_rep_clean) < 6:
            raise AdminServiceError("INVALID_LEGAL_REP_DOC", f"La Cédula del Representante Legal '{data.legal_rep_doc}' no es válida (debe tener al menos 6 dígitos numéricos).", 400)

    full_name = (data.full_name or "").strip()
    if not full_name:
        raise AdminServiceError("MISSING_NAME", "El nombre del cliente no puede estar vacío.", 400)

    phone = (data.phone or "").strip()
    if not phone:
        raise AdminServiceError("MISSING_PHONE", "El teléfono no puede estar vacío.", 400)

    income_source = None
    if data.income_source:
        inc_raw = data.income_source.upper().strip()
        if inc_raw in INCOME_SOURCE_BY_TIPO:
            income_source = INCOME_SOURCE_BY_TIPO[inc_raw]
        elif inc_raw in (INCOME_SOURCE_DIAN, INCOME_SOURCE_MANUAL_SALES):
            income_source = inc_raw
        else:
            raise AdminServiceError("INVALID_INCOME_SOURCE", f"Módulo '{data.income_source}' no reconocido. Debe ser FACTURADOR o VENTAS_MANUALES.", 400)

    taxpayer_type = "PERSONA_JURIDICA" if tipo_clean == "EMPRESA" else "PERSONA_NATURAL"
    business_name = data.business_name.strip() if tipo_clean == "EMPRESA" and data.business_name else full_name
    dv = calculate_dian_dv(nit_clean)

    existing_biz = db.query(Business).filter(Business.nit == nit_clean).first()
    if not allow_existing_nit and existing_biz:
        raise AdminServiceError("NIT_ALREADY_EXISTS", f"Ya existe un negocio registrado con el NIT {nit_clean}.", 409)
    existing_user = None
    if existing_biz and existing_biz.client and existing_biz.client.role == "CLIENT":
        existing_user = existing_biz.client
    else:
        existing_user = db.query(User).filter(
            ((User.phone == phone) | (User.full_name == full_name)) & (User.role == "CLIENT")
        ).first()

    try:
        if existing_user:
            user = existing_user
            user.phone = phone
            user.full_name = full_name
            business = existing_biz or db.query(Business).filter(Business.client_id == user.id).first()
            if not business:
                business = Business(
                    client_id=user.id,
                    legal_name=business_name,
                    commercial_name=business_name,
                    nit=nit_clean,
                    dv=dv,
                    taxpayer_type=taxpayer_type,
                    legal_rep_doc=legal_rep_clean,
                    income_source=income_source or INCOME_SOURCE_DIAN,
                    is_active=True,
                )
                db.add(business)
            else:
                business.client_id = user.id
                business.legal_name = business_name
                business.commercial_name = business_name
                business.nit = nit_clean
                business.dv = dv
                business.taxpayer_type = taxpayer_type
                business.legal_rep_doc = legal_rep_clean
                if income_source:
                    business.income_source = income_source
        else:
            # Generar email unívoco garantizado
            email_prefix = re.sub(r"[^a-zA-Z0-9]", ".", full_name.lower().strip())
            email_candidate = f"{email_prefix}@cliente.kontable.co"
            suffix = 1
            while db.query(User).filter(User.email == email_candidate).first():
                email_candidate = f"{email_prefix}.{nit_clean[-4:]}.{suffix}@cliente.kontable.co"
                suffix += 1

            user = User(
                email=email_candidate,
                full_name=full_name,
                phone=phone,
                role="CLIENT",
                is_active=True,
                is_telegram_linked=False,
            )
            db.add(user)
            db.flush()

            if existing_biz:
                business = existing_biz
                business.client_id = user.id
                business.legal_name = business_name
                business.commercial_name = business_name
                business.nit = nit_clean
                business.dv = dv
                business.taxpayer_type = taxpayer_type
                business.legal_rep_doc = legal_rep_clean
                if income_source:
                    business.income_source = income_source
            else:
                business = Business(
                    client_id=user.id,
                    legal_name=business_name,
                    commercial_name=business_name,
                    nit=nit_clean,
                    dv=dv,
                    taxpayer_type=taxpayer_type,
                    legal_rep_doc=legal_rep_clean,
                    income_source=income_source or INCOME_SOURCE_DIAN,
                    is_active=True,
                )
                db.add(business)

        # Crear o renovar Suscripción con Descuento desde tarifa vigente
        target_income_source = income_source or (existing_biz.income_source if existing_biz else INCOME_SOURCE_DIAN)
        try:
            quote = PricingService.quote_for_segment(
                db=db,
                income_source=target_income_source,
                taxpayer_type=taxpayer_type,
                plan_code=plan_obj.code,
                start_date=date.today(),
            )
        except PricingError as pe:
            raise AdminServiceError(pe.code, pe.message, pe.status_code) from pe

        subscription = db.query(Subscription).filter(Subscription.client_id == user.id).first()
        if not subscription:
            subscription = Subscription(
                client_id=user.id,
                plan=quote.plan,
                monthly_price=quote.monthly_price,
                price_origin=PRICE_ORIGIN_TARIFA,
                price_note=None,
                discount_rate=quote.discount_rate,
                base_price=quote.gross_total,
                final_price=quote.final_price,
                start_date=quote.start_date,
                cutoff_date=quote.cutoff_date,
                grace_period_end=quote.grace_period_end,
                status="ACTIVO",
            )
            db.add(subscription)
        else:
            subscription.plan = quote.plan
            subscription.monthly_price = quote.monthly_price
            subscription.price_origin = PRICE_ORIGIN_TARIFA
            subscription.price_note = None
            subscription.discount_rate = quote.discount_rate
            subscription.base_price = quote.gross_total
            subscription.final_price = quote.final_price
            subscription.start_date = quote.start_date
            subscription.cutoff_date = quote.cutoff_date
            subscription.grace_period_end = quote.grace_period_end
            subscription.status = "ACTIVO"

        db.commit()
        db.refresh(user)
        db.refresh(business)

        # Generar Token y Enlace Mágico Deep Linking (72 horas)
        token_record, deep_link_url = TelegramDeepLinkingService.generate_link_token(
            user_id=user.id,
            db=db,
            expires_in_hours=72,
            bot_username=bot_username,
        )

        return CreatedClient(
            user=user,
            business=business,
            subscription=subscription,
            deep_link_url=deep_link_url,
            tipo_cliente=tipo_clean,
            nit_with_dv=f"{nit_clean}-{dv}",
            final_price=float(quote.final_price),
            discount_rate=float(quote.discount_rate),
            cutoff_date=quote.cutoff_date,
            grace_period_end=quote.grace_period_end,
            monthly_price=quote.monthly_price,
        )
    except AdminServiceError:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error registrando cliente: {e}", exc_info=True)
        raise AdminServiceError("INTERNAL_ERROR", f"Error interno al crear el cliente: {str(e)}", 500)


def confirm_payment(
    db: Session,
    admin: Optional[User],
    nit: str,
    amount: Any,
    reference: str,
    notifier: Optional[Callable[[int, str], bool]] = None,
    allow_mismatch: bool = True,
) -> PaymentResult:
    """Registra un pago comercial, reactiva la suscripción y notifica al cliente si está vinculado."""
    nit_clean = normalize_nit(nit) or "".join(filter(str.isdigit, str(nit)))
    if len(nit_clean) < 6:
        raise AdminServiceError("INVALID_NIT", f"El NIT '{nit}' no es válido.", 400)

    try:
        amount_val = Decimal(str(amount))
    except Exception:
        raise AdminServiceError("INVALID_AMOUNT", f"Monto '{amount}' inválido. Ingresa un valor numérico.", 400)

    if amount_val <= Decimal("0"):
        raise AdminServiceError("INVALID_AMOUNT", "El monto del pago debe ser mayor a cero.", 400)

    ref_code = (reference or "").strip()
    if not ref_code:
        raise AdminServiceError("INVALID_REFERENCE", "La referencia del pago no puede estar vacía.", 400)

    biz = db.query(Business).filter(Business.nit == nit_clean).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio registrado con NIT {nit_clean}.", 404)

    client = biz.client
    if not client:
        raise AdminServiceError("CLIENT_NOT_FOUND", f"No existe usuario cliente asociado al negocio '{biz.commercial_name}'.", 404)

    sub = (
        db.query(Subscription)
        .filter(Subscription.client_id == client.id)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    if not sub:
        raise AdminServiceError("SUBSCRIPTION_NOT_FOUND", f"El cliente {client.full_name} no tiene ninguna suscripción registrada.", 404)

    expected_amount = Decimal(str(sub.final_price))
    difference = amount_val - expected_amount

    if amount_val != expected_amount and not allow_mismatch:
        raise AdminServiceError(
            "AMOUNT_MISMATCH",
            f"El valor del periodo es ${expected_amount:,.0f} y el pago es ${amount_val:,.0f} (diferencia ${difference:,.0f}).",
            409,
            extra={
                "expected_amount": f"{expected_amount:.2f}",
                "amount": f"{amount_val:.2f}",
                "difference": f"{difference:.2f}",
            },
        )

    try:
        admin_id = admin.id if admin else None
        payment = PaymentRecord(
            subscription_id=sub.id,
            amount=amount_val,
            expected_amount=expected_amount,
            payment_date=date.today(),
            payment_method="TRANSFERENCIA",
            reference_code=ref_code,
            verified_by_admin_id=admin_id,
            notes=f"Pago confirmado por admin {admin.full_name if admin else admin_id}",
        )
        db.add(payment)

        today = date.today()
        plan_obj = db.query(PricingPlan).filter(PricingPlan.code == sub.plan).first()
        months_to_add = plan_obj.months if plan_obj else 3

        base_renewal_date = sub.cutoff_date if sub.cutoff_date >= today else today
        new_cutoff = add_months_to_date(base_renewal_date, months_to_add)
        grace_days = PricingService.get_grace_days(db)
        new_grace_end = new_cutoff + timedelta(days=grace_days)

        sub.cutoff_date = new_cutoff
        sub.grace_period_end = new_grace_end
        sub.status = "ACTIVO"
        sub.last_notified_at = None

        db.commit()
        db.refresh(payment)
        db.refresh(sub)

        client_reactivation_msg = (
            "🎉 *¡Pago Confirmado y Servicio Reactivado!*\n\n"
            f"Hola *{client.full_name}*, tu pago de *${amount_val:,.0f} COP* (Ref: `{ref_code}`) "
            "ha sido verificado y registrado exitosamente por la administración comercial.\n\n"
            f"✅ Tu suscripción para *{biz.commercial_name}* se encuentra **100% ACTIVA**.\n"
            f"📅 *Nuevo próximo corte:* {sub.cutoff_date.strftime('%d/%m/%Y')}\n"
            f"⏳ *Periodo de gracia hasta:* {sub.grace_period_end.strftime('%d/%m/%Y')}\n\n"
            "🔓 El acceso a la plataforma web y las consultas en este bot de Telegram han sido "
            f"completamente restablecidos. ¡Gracias por confiar en {BRAND_NAME}!"
        )

        client_notified = False
        if client.is_telegram_linked and client.telegram_chat_id:
            if notifier:
                try:
                    client_notified = bool(notifier(client.telegram_chat_id, client_reactivation_msg))
                except Exception as e:
                    logger.error(f"Error enviando notificación de reactivación al cliente {client.telegram_chat_id}: {e}")
                    client_notified = False
            else:
                client_notified = True

        return PaymentResult(
            payment=payment,
            subscription=sub,
            client=client,
            business=biz,
            new_cutoff_date=sub.cutoff_date,
            client_notified=client_notified,
            expected_amount=expected_amount,
            difference=difference,
        )
    except AdminServiceError:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error registrando pago para NIT {nit_clean}: {e}", exc_info=True)
        raise AdminServiceError("INTERNAL_ERROR", f"Error interno al registrar el pago: {str(e)}", 500)


def set_income_source(
    db: Session,
    admin: Optional[User],
    nit: str,
    income_source: str,
) -> Business:
    """Cambia el tipo de negocio (DIAN o ventas manuales) sin modificar la suscripción."""
    nit_clean = normalize_nit(nit) or "".join(filter(str.isdigit, str(nit)))
    if len(nit_clean) < 6:
        raise AdminServiceError("INVALID_NIT", f"El NIT '{nit}' no es válido (debe tener al menos 6 dígitos numéricos).", 400)

    src_upper = (income_source or "").upper().strip()
    if src_upper in INCOME_SOURCE_BY_TIPO:
        mapped_source = INCOME_SOURCE_BY_TIPO[src_upper]
    elif src_upper in (INCOME_SOURCE_DIAN, INCOME_SOURCE_MANUAL_SALES):
        mapped_source = src_upper
    else:
        raise AdminServiceError("INVALID_INCOME_SOURCE", f"Tipo '{income_source}' no reconocido.", 400)

    biz = db.query(Business).filter(Business.nit == nit_clean).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio registrado con NIT {nit_clean}.", 404)

    try:
        biz.income_source = mapped_source
        db.commit()
        db.refresh(biz)
        return biz
    except Exception as e:
        db.rollback()
        logger.error(f"Error cambiando tipo del NIT {nit_clean}: {e}", exc_info=True)
        raise AdminServiceError("INTERNAL_ERROR", f"Error interno al cambiar el tipo: {str(e)}", 500)


def set_tax_profile(
    db: Session,
    admin: Optional[User],
    nit: str,
    iva_periodicity: Optional[str],
    is_withholding_agent: bool,
) -> Business:
    """Fija la periodicidad de IVA y si es agente de retención en un negocio facturador (DIAN)."""
    nit_clean = normalize_nit(nit) or "".join(filter(str.isdigit, str(nit)))
    if len(nit_clean) < 6:
        raise AdminServiceError("INVALID_NIT", f"El NIT '{nit}' no es válido (debe tener al menos 6 dígitos numéricos).", 400)

    if iva_periodicity is not None:
        clean_iva = iva_periodicity.upper().strip()
        if clean_iva in ("NINGUNO", "NONE", ""):
            clean_iva = None
        elif clean_iva in (IVA_PERIODICITY_BIMESTRAL, IVA_PERIODICITY_CUATRIMESTRAL):
            pass
        else:
            raise AdminServiceError("INVALID_IVA_PERIODICITY", f"Periodicidad de IVA '{iva_periodicity}' no reconocida.", 400)
    else:
        clean_iva = None

    biz = db.query(Business).filter(Business.nit == nit_clean).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio registrado con NIT {nit_clean}.", 404)

    if biz.income_source != INCOME_SOURCE_DIAN:
        raise AdminServiceError(
            "NOT_APPLICABLE",
            f"{biz.commercial_name} registra sus ventas a mano: no tiene IVA, ICA ni otros impuestos que configurar. El perfil tributario aplica solo a negocios facturadores.",
            400,
        )

    try:
        biz.iva_periodicity = clean_iva
        biz.is_withholding_agent = bool(is_withholding_agent)
        db.commit()
        db.refresh(biz)
        return biz
    except Exception as e:
        db.rollback()
        logger.error(f"Error guardando perfil tributario del NIT {nit_clean}: {e}", exc_info=True)
        raise AdminServiceError("INTERNAL_ERROR", f"Error interno al guardar el perfil: {str(e)}", 500)


def release_telegram(
    db: Session,
    admin: Optional[User],
    chat_id: int,
) -> User:
    """Libera la vinculación de Telegram de un usuario."""
    if admin and getattr(admin, "telegram_chat_id", None) and admin.telegram_chat_id == chat_id:
        raise AdminServiceError("CANNOT_FREE_SELF", "No puedes desvincular tu propia cuenta de administrador.", 400)

    user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
    if not user:
        raise AdminServiceError("USER_NOT_FOUND", "No hay ninguna cuenta vinculada a ese ID.", 404)

    try:
        user.telegram_chat_id = None
        user.telegram_username = None
        user.is_telegram_linked = False
        db.commit()
        db.refresh(user)
        return user
    except Exception as e:
        db.rollback()
        logger.error(f"Error liberando Telegram para chat_id={chat_id}: {e}", exc_info=True)
        raise AdminServiceError("INTERNAL_ERROR", f"Error interno al desvincular la cuenta: {str(e)}", 500)


def enqueue_extraction(
    db: Session,
    admin: Optional[User],
    nit: str,
    period_spec: Optional[str] = None,
) -> DIANExtractionJob:
    """Encola un trabajo de extracción DIAN para el negocio especificado por NIT."""
    nit_clean = normalize_nit(nit) or "".join(filter(str.isdigit, str(nit)))
    if len(nit_clean) < 6:
        raise AdminServiceError("INVALID_NIT", f"El NIT '{nit}' no es válido (debe tener al menos 6 dígitos numéricos).", 400)

    period = period_spec
    if not period:
        today = date.today()
        year, month = today.year, today.month - 1
        if month == 0:
            month, year = 12, year - 1
        period = f"{year:04d}-{month:02d}"

    biz = db.query(Business).filter(Business.nit == nit_clean).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio registrado con NIT {nit_clean}.", 404)

    from dian_automation.queue.manager import ExtractionQueueManager

    try:
        job = ExtractionQueueManager.enqueue_job(business_id=biz.id, target_period=period, db=db)
        return job
    except Exception as e:
        logger.error(f"Error encolando extracción para NIT {nit_clean}: {e}", exc_info=True)
        raise AdminServiceError("INTERNAL_ERROR", f"Error interno al encolar la extracción: {str(e)}", 500)


def list_clients(
    db: Session,
    admin: Optional[User] = None,
    limit: int = 10,
    offset: int = 0,
) -> List[User]:
    """Lista usuarios clientes ordenados por fecha de creación descendente."""
    return (
        db.query(User)
        .filter(User.role == "CLIENT")
        .order_by(User.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def new_activation_link(
    db: Session,
    admin: Optional[User],
    user_id: str,
    bot_username: str = "KontaBot",
) -> str:
    """Invalida los enlaces de vinculación no usados anteriores del usuario y genera uno nuevo."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise AdminServiceError("USER_NOT_FOUND", f"No se encontró ningún usuario con ID '{user_id}'.", 404)

    try:
        # Invalida los enlaces anteriores no usados del usuario
        db.query(TelegramLinkToken).filter(
            TelegramLinkToken.user_id == user.id,
            TelegramLinkToken.is_used.is_(False),
        ).update({"is_used": True}, synchronize_session=False)
        db.commit()

        # Genera un nuevo token y enlace
        token_record, deep_link_url = TelegramDeepLinkingService.generate_link_token(
            user_id=user.id,
            db=db,
            expires_in_hours=72,
            bot_username=bot_username,
        )
        return deep_link_url
    except Exception as e:
        db.rollback()
        logger.error(f"Error generando nuevo enlace de activación para user {user_id}: {e}", exc_info=True)
        raise AdminServiceError("INTERNAL_ERROR", f"Error interno al generar enlace de activación: {str(e)}", 500)


def _normalize_search_term(s: str) -> str:
    """Normaliza texto removiendo acentos/tildes y convirtiendo a minúsculas."""
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _unaccent_col(col):
    """Construye expresión SQL para reemplazar vocales acentuadas por sus formas simples.

    Compatible de forma nativa tanto con SQLite como con PostgreSQL sin requerir extensiones externas.
    Permite búsquedas insensibles a tildes con func.lower y func.replace.
    """
    expr = func.lower(col)
    # lower() de SQLite solo convierte ASCII: las mayúsculas con tilde se reemplazan aparte. La ñ se
    # reduce a n porque el término de búsqueda pasa por NFKD, que también le quita la virgulilla.
    for accented, plain in [
        ("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u"), ("ñ", "n"),
        ("Á", "a"), ("É", "e"), ("Í", "i"), ("Ó", "o"), ("Ú", "u"), ("Ü", "u"), ("Ñ", "n"),
    ]:
        expr = func.replace(expr, accented, plain)
    return expr


def list_clients_paged(
    db: Session,
    admin: Optional[User] = None,
    q: Optional[str] = None,
    income_source: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
) -> Dict[str, Any]:
    """Lista clientes de forma paginada con filtros por texto, origen de ingresos y estado de suscripción (AC #1).

    Ordenado alfabéticamente por nombre comercial.
    """
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    offset = (page - 1) * page_size

    # Correlacionamos la suscripción más reciente por usuario
    latest_sub_id = (
        db.query(Subscription.id)
        .filter(Subscription.client_id == Business.client_id)
        .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        .limit(1)
        .correlate(Business)
        .scalar_subquery()
    )

    query = (
        db.query(Business)
        .join(User, Business.client_id == User.id)
        .outerjoin(Subscription, Subscription.id == latest_sub_id)
        .filter(User.role == "CLIENT")
    )

    if q and q.strip():
        q_clean = q.strip()
        q_norm = _normalize_search_term(q_clean)
        # Búsqueda insensible a mayúsculas y acentos compatible con SQLite y PostgreSQL
        search_clauses = [
            _unaccent_col(User.full_name).contains(q_norm),
            _unaccent_col(Business.legal_name).contains(q_norm),
            _unaccent_col(Business.commercial_name).contains(q_norm),
            Business.nit.contains(q_clean),
            User.phone.contains(q_clean),
            User.full_name.ilike(f"%{q_clean}%"),
            Business.legal_name.ilike(f"%{q_clean}%"),
            Business.commercial_name.ilike(f"%{q_clean}%"),
        ]
        query = query.filter(or_(*search_clauses))

    if income_source and income_source.strip():
        inc_clean = income_source.upper().strip()
        if inc_clean in INCOME_SOURCE_BY_TIPO:
            inc_clean = INCOME_SOURCE_BY_TIPO[inc_clean]
        query = query.filter(Business.income_source == inc_clean)

    if status and status.strip():
        st_clean = status.upper().strip()
        query = query.filter(Subscription.status == st_clean)

    total = query.count()

    results = (
        query
        .add_columns(User, Subscription)
        .order_by(func.lower(Business.commercial_name).asc(), Business.id.asc())
        .offset(offset)
        .limit(page_size)
        .all()
    )

    items = []
    for biz, user, sub in results:
        items.append({
            "business_id": biz.id,
            "user_id": user.id,
            "legal_name": biz.legal_name,
            "commercial_name": biz.commercial_name,
            "nit": biz.nit,
            "dv": biz.dv,
            "income_source": biz.income_source,
            "taxpayer_type": biz.taxpayer_type,
            "contact_name": user.full_name,
            "phone": user.phone,
            "is_telegram_linked": bool(user.is_telegram_linked),
            "plan": sub.plan if sub else None,
            "subscription_status": sub.status if sub else None,
            "cutoff_date": sub.cutoff_date.isoformat() if sub and sub.cutoff_date else None,
            "grace_period_end": sub.grace_period_end.isoformat() if sub and sub.grace_period_end else None,
            "is_active": bool(biz.is_active),
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_client_detail(
    db: Session,
    business_id: str,
    admin: Optional[User] = None,
) -> Dict[str, Any]:
    """Obtiene la ficha técnica y comercial completa de un negocio (Story 8.3 - AC #2)."""
    biz = db.query(Business).filter(
        (Business.id == business_id) | (Business.nit == business_id)
    ).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio con identificador '{business_id}'.", 404)

    user = biz.client or db.query(User).filter(User.id == biz.client_id).first()
    if not user:
        raise AdminServiceError("CLIENT_NOT_FOUND", "No se encontró el usuario cliente asociado al negocio.", 404)

    # Suscripción más reciente del usuario
    sub = (
        db.query(Subscription)
        .filter(Subscription.client_id == user.id)
        .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        .first()
    )

    subscription_info = None
    if sub:
        current_rate_monthly = None
        try:
            rate = PricingService.current_rate(db, biz.income_source, biz.taxpayer_type)
            current_rate_monthly = f"{rate.monthly_price:.2f}"
        except Exception:
            pass

        price_changes_records = (
            db.query(SubscriptionPriceChange)
            .filter(SubscriptionPriceChange.subscription_id == sub.id)
            .order_by(SubscriptionPriceChange.created_at.desc(), SubscriptionPriceChange.id.desc())
            .limit(10)
            .all()
        )
        price_changes = [
            {
                "id": ch.id,
                "created_at": ch.created_at.isoformat() if ch.created_at else None,
                "old_plan": ch.old_plan,
                "new_plan": ch.new_plan,
                "old_monthly_price": f"{ch.old_monthly_price:.2f}" if ch.old_monthly_price is not None else None,
                "new_monthly_price": f"{ch.new_monthly_price:.2f}" if ch.new_monthly_price is not None else None,
                "old_discount_rate": f"{ch.old_discount_rate:.2f}" if ch.old_discount_rate is not None else None,
                "new_discount_rate": f"{ch.new_discount_rate:.2f}" if ch.new_discount_rate is not None else None,
                "old_final_price": f"{ch.old_final_price:.2f}" if ch.old_final_price is not None else None,
                "new_final_price": f"{ch.new_final_price:.2f}" if ch.new_final_price is not None else None,
                "reason": ch.reason,
                "admin": ch.admin.full_name if ch.admin else None,
            }
            for ch in price_changes_records
        ]

        subscription_info = {
            "id": sub.id,
            "plan": sub.plan,
            "status": sub.status,
            "discount_rate": f"{sub.discount_rate:.2f}" if sub.discount_rate is not None else None,
            "base_price": f"{sub.base_price:.2f}" if sub.base_price is not None else None,
            "final_price": f"{sub.final_price:.2f}" if sub.final_price is not None else None,
            "monthly_price": f"{sub.monthly_price:.2f}" if sub.monthly_price is not None else None,
            "price_origin": sub.price_origin or PRICE_ORIGIN_TARIFA,
            "price_note": sub.price_note,
            "current_rate_monthly": current_rate_monthly,
            "price_changes": price_changes,
            "start_date": sub.start_date.isoformat() if sub.start_date else None,
            "cutoff_date": sub.cutoff_date.isoformat() if sub.cutoff_date else None,
            "grace_period_end": sub.grace_period_end.isoformat() if sub.grace_period_end else None,
        }

    # Últimos 20 pagos del cliente
    payments_records = (
        db.query(PaymentRecord)
        .join(Subscription, PaymentRecord.subscription_id == Subscription.id)
        .filter(Subscription.client_id == user.id)
        .order_by(PaymentRecord.payment_date.desc(), PaymentRecord.created_at.desc())
        .limit(20)
        .all()
    )
    recent_payments = [
        {
            "id": p.id,
            "payment_date": p.payment_date.isoformat() if p.payment_date else None,
            "amount": f"{p.amount:.2f}",
            "expected_amount": f"{p.expected_amount:.2f}" if p.expected_amount is not None else None,
            "reference_code": p.reference_code,
            "payment_method": p.payment_method,
            "verified_by_admin_id": p.verified_by_admin_id,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in payments_records
    ]

    # Últimas 10 extracciones DIAN
    extractions = (
        db.query(DIANExtractionJob)
        .filter(DIANExtractionJob.business_id == biz.id)
        .order_by(DIANExtractionJob.created_at.desc(), DIANExtractionJob.id.desc())
        .limit(10)
        .all()
    )
    recent_extractions = [
        {
            "id": job.id,
            "period": job.target_period,
            "status": job.status,
            "attempts": job.attempt_count,
            "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "error_code": job.error_code,
        }
        for job in extractions
    ]

    # Cantidad de documentos activos
    active_docs_count = (
        db.query(BusinessDocument)
        .filter(
            BusinessDocument.business_id == biz.id,
            BusinessDocument.deleted_at.is_(None),
        )
        .count()
    )

    # Enlace de activación pendiente
    now = datetime.utcnow()
    has_pending_link = (
        db.query(TelegramLinkToken)
        .filter(
            TelegramLinkToken.user_id == user.id,
            TelegramLinkToken.is_used.is_(False),
            TelegramLinkToken.expires_at > now,
        )
        .first()
        is not None
    )

    return {
        "business": {
            "id": biz.id,
            "legal_name": biz.legal_name,
            "commercial_name": biz.commercial_name,
            "nit": biz.nit,
            "dv": biz.dv,
            "taxpayer_type": biz.taxpayer_type,
            "legal_rep_doc": biz.legal_rep_doc,
            "economic_activity": biz.economic_activity,
            "income_source": biz.income_source,
            "is_active": bool(biz.is_active),
            "created_at": biz.created_at.isoformat() if biz.created_at else None,
        },
        "contact": {
            "id": user.id,
            "full_name": user.full_name,
            "phone": user.phone,
            "email": user.email,
            "is_telegram_linked": bool(user.is_telegram_linked),
            "telegram_chat_id": user.telegram_chat_id,
            "telegram_username": user.telegram_username,
        },
        "tax_profile": {
            "iva_periodicity": biz.iva_periodicity,
            "is_withholding_agent": bool(biz.is_withholding_agent),
        },
        "subscription": subscription_info,
        "recent_payments": recent_payments,
        "recent_extractions": recent_extractions,
        "active_documents_count": active_docs_count,
        "has_pending_activation_link": has_pending_link,
        # Conveniencia top-level
        "business_id": biz.id,
        "legal_name": biz.legal_name,
        "commercial_name": biz.commercial_name,
        "nit": biz.nit,
        "dv": biz.dv,
        "income_source": biz.income_source,
        "taxpayer_type": biz.taxpayer_type,
        "contact_name": user.full_name,
        "phone": user.phone,
        "is_telegram_linked": bool(user.is_telegram_linked),
        "telegram_chat_id": user.telegram_chat_id,
        "iva_periodicity": biz.iva_periodicity,
        "is_withholding_agent": bool(biz.is_withholding_agent),
    }


def release_client_telegram(
    db: Session,
    admin: Optional[User],
    business_id: str,
) -> User:
    """Desvincula la cuenta de Telegram del cliente dueño del negocio especificado (Story 8.3 - AC #7)."""
    biz = db.query(Business).filter(
        (Business.id == business_id) | (Business.nit == business_id)
    ).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio con identificador '{business_id}'.", 404)

    user = biz.client or db.query(User).filter(User.id == biz.client_id).first()
    if not user:
        raise AdminServiceError("CLIENT_NOT_FOUND", "No se encontró el usuario cliente asociado al negocio.", 404)

    if admin and user.id == admin.id:
        raise AdminServiceError("CANNOT_FREE_SELF", "No puedes desvincular tu propia cuenta de administrador.", 400)

    if admin and getattr(admin, "telegram_chat_id", None) and user.telegram_chat_id == admin.telegram_chat_id:
        raise AdminServiceError("CANNOT_FREE_SELF", "No puedes desvincular tu propia cuenta de administrador.", 400)

    try:
        user.telegram_chat_id = None
        user.telegram_username = None
        user.is_telegram_linked = False
        db.commit()
        db.refresh(user)
        return user
    except Exception as e:
        db.rollback()
        logger.error(f"Error liberando Telegram para user_id={user.id}: {e}", exc_info=True)
        raise AdminServiceError("INTERNAL_ERROR", f"Error interno al desvincular la cuenta: {str(e)}", 500)


# ==============================================================================
# Story 8.4: Resumen Comercial, Estado del Worker y Trabajos DIAN
# ==============================================================================

KNOWN_JOB_ERROR_MESSAGES: Dict[str, str] = {
    "AUTH_FAILED": "Fallo de autenticación en portal DIAN",
    "AuthFailedError": "Fallo de autenticación en portal DIAN",
    "MAIL_TIMEOUT": "Tiempo de espera agotado buscando token en correo",
    "MailTokenTimeoutError": "Tiempo de espera agotado buscando token en correo",
    "TURNSTILE_BLOCKED": "Bloqueo o verificación fallida en Cloudflare Turnstile",
    "TurnstileBlockedError": "Bloqueo o verificación fallida en Cloudflare Turnstile",
    "DIAN_DOWN": "El portal DIAN VPFE no responde o se encuentra en mantenimiento",
    "DIANPortalDownError": "El portal DIAN VPFE no responde o se encuentra en mantenimiento",
    "EXTRACTION_ERROR": "Error durante la extracción en la DIAN",
    "DIANExtractionError": "Error durante la extracción en la DIAN",
}


def format_extraction_error_message(error_code: Optional[str]) -> Optional[str]:
    """Genera mensaje legible en español para códigos de error de extracción (Story 8.4 - AC #2)."""
    if not error_code:
        return None
    code_clean = str(error_code).strip()
    if is_slow_error(code_clean):
        return _cause_text(code_clean)
    if code_clean in KNOWN_JOB_ERROR_MESSAGES:
        return KNOWN_JOB_ERROR_MESSAGES[code_clean]
    return f"Error en la descarga ({code_clean})"


def get_worker_status(db: Session) -> Dict[str, Any]:
    """Estado y latidos del worker remoto frente al umbral de silencio (Story 8.4 - AC #3)."""
    silence_threshold = getattr(config, "worker_silence_minutes", 30)
    rows = db.query(WorkerHeartbeat).order_by(WorkerHeartbeat.name.asc()).all()
    now_utc = datetime.utcnow()
    workers = []
    for r in rows:
        if r.last_seen_at is not None:
            delta = now_utc - r.last_seen_at
            minutes_since = max(0, int(delta.total_seconds() // 60))
            is_silent = delta > timedelta(minutes=silence_threshold)
            last_seen_iso = r.last_seen_at.isoformat()
        else:
            minutes_since = None
            is_silent = True
            last_seen_iso = None
        workers.append({
            "name": r.name,
            "last_seen_at": last_seen_iso,
            "minutes_since": minutes_since,
            "is_silent": is_silent,
        })
    return {
        "silence_threshold_minutes": silence_threshold,
        "workers": workers,
    }


def list_jobs_paged(
    db: Session,
    admin: Optional[User] = None,
    status: Optional[str] = None,
    business_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
) -> Dict[str, Any]:
    """Listado paginado de trabajos de extracción DIAN más recientes primero (Story 8.4 - AC #2)."""
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    offset = (page - 1) * page_size

    query = db.query(DIANExtractionJob, Business).join(Business, DIANExtractionJob.business_id == Business.id)

    if status and status.strip():
        query = query.filter(DIANExtractionJob.status == status.upper().strip())

    if business_id and business_id.strip():
        biz_clean = business_id.strip()
        query = query.filter(or_(DIANExtractionJob.business_id == biz_clean, Business.nit == biz_clean))

    total = query.count()

    results = (
        query
        .order_by(DIANExtractionJob.created_at.desc(), DIANExtractionJob.id.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )

    items = []
    for job, biz in results:
        items.append({
            "job_id": job.id,
            "business_id": biz.id,
            "commercial_name": biz.commercial_name,
            "nit": biz.nit,
            "target_period": job.target_period,
            "status": job.status,
            "attempt_count": job.attempt_count,
            "max_attempts": job.max_attempts,
            "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "error_code": job.error_code,
            "error_message": format_extraction_error_message(job.error_code),
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_admin_summary(
    db: Session,
    admin: Optional[User] = None,
    now_bogota: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Resumen consolidado de cartera, operaciones y estado del worker (Story 8.4 - AC #1)."""
    if now_bogota is None:
        now_bogota = datetime.now(ZoneInfo("America/Bogota"))
    today_bogota = now_bogota.date()
    cutoff_horizon = today_bogota + timedelta(days=7)

    latest_sub_id = (
        db.query(Subscription.id)
        .filter(Subscription.client_id == Business.client_id)
        .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        .limit(1)
        .correlate(Business)
        .scalar_subquery()
    )

    businesses = (
        db.query(Business, Subscription)
        .join(User, Business.client_id == User.id)
        .outerjoin(Subscription, Subscription.id == latest_sub_id)
        .filter(User.role == "CLIENT")
        .all()
    )

    clients_by_status = {
        "ACTIVO": 0,
        "EN_MORA": 0,
        "BLOQUEADO": 0,
        "CANCELADO": 0,
        "SIN_SUSCRIPCION": 0,
    }
    clients_by_income_source = {
        "DIAN": 0,
        "MANUAL_SALES": 0,
    }
    upcoming_cutoffs = []
    in_grace = []

    for biz, sub in businesses:
        # Origen de ingresos
        if biz.income_source == INCOME_SOURCE_MANUAL_SALES:
            clients_by_income_source["MANUAL_SALES"] += 1
        else:
            clients_by_income_source["DIAN"] += 1

        # Estado de suscripción
        if not sub:
            clients_by_status["SIN_SUSCRIPCION"] += 1
        elif sub.status in clients_by_status:
            clients_by_status[sub.status] += 1
        else:
            clients_by_status[sub.status] = clients_by_status.get(sub.status, 0) + 1

        # Próximos cortes (próximos 7 días, hora Bogotá)
        if sub and sub.status == "ACTIVO" and sub.cutoff_date and today_bogota <= sub.cutoff_date <= cutoff_horizon:
            upcoming_cutoffs.append({
                "business_id": biz.id,
                "commercial_name": biz.commercial_name,
                "nit": biz.nit,
                "cutoff_date": sub.cutoff_date.isoformat(),
            })

        # Periodo de gracia (en mora o ventana activa de gracia)
        is_in_grace = False
        if sub:
            if sub.status == "EN_MORA":
                is_in_grace = True
            elif (
                sub.status == "ACTIVO"
                and sub.cutoff_date
                and sub.grace_period_end
                and sub.cutoff_date <= today_bogota <= sub.grace_period_end
            ):
                is_in_grace = True

        if is_in_grace and sub:
            in_grace.append({
                "business_id": biz.id,
                "commercial_name": biz.commercial_name,
                "nit": biz.nit,
                "cutoff_date": sub.cutoff_date.isoformat() if sub.cutoff_date else None,
                "grace_period_end": sub.grace_period_end.isoformat() if sub.grace_period_end else None,
            })

    upcoming_cutoffs.sort(key=lambda x: x["cutoff_date"])
    in_grace.sort(key=lambda x: x["grace_period_end"] or "")

    # Pagos de este mes (hora Bogotá)
    cur_year = today_bogota.year
    cur_month = today_bogota.month
    start_of_month = date(cur_year, cur_month, 1)
    last_day_num = calendar.monthrange(cur_year, cur_month)[1]
    end_of_month = date(cur_year, cur_month, last_day_num)

    payments = (
        db.query(PaymentRecord)
        .filter(PaymentRecord.payment_date >= start_of_month, PaymentRecord.payment_date <= end_of_month)
        .all()
    )
    payments_count = len(payments)
    payments_total = float(sum(p.amount for p in payments))

    # Trabajos fallidos en últimas 24h
    since_24h = datetime.utcnow() - timedelta(hours=24)
    failed_jobs_24h = (
        db.query(DIANExtractionJob)
        .filter(
            DIANExtractionJob.status == "FAILED",
            or_(
                DIANExtractionJob.finished_at >= since_24h,
                and_(DIANExtractionJob.finished_at.is_(None), DIANExtractionJob.created_at >= since_24h),
            ),
        )
        .count()
    )

    # Clientes sin Telegram vinculado
    unlinked_telegram = (
        db.query(User)
        .filter(
            User.role == "CLIENT",
            or_(User.is_telegram_linked.is_(False), User.telegram_chat_id.is_(None)),
        )
        .count()
    )

    # Estado del worker
    worker_data = get_worker_status(db)

    return {
        "clients_by_status": clients_by_status,
        "clients_by_income_source": clients_by_income_source,
        "upcoming_cutoffs": upcoming_cutoffs,
        "in_grace": in_grace,
        "payments_this_month": {
            "count": payments_count,
            "total": payments_total,
        },
        "failed_jobs_24h": failed_jobs_24h,
        "unlinked_telegram": unlinked_telegram,
        "worker": worker_data,
    }


