"""Servicio de dominio para operaciones de administración comercial.

Compartido entre el bot de Telegram y el panel web de administración.
No conoce chat_id, Markdown ni Telegram (desacoplado de la capa de presentación).
"""

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional
from sqlalchemy.orm import Session

from dian_automation.branding import BRAND_NAME
from dian_automation.db.models import (
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    IVA_PERIODICITY_BIMESTRAL,
    IVA_PERIODICITY_CUATRIMESTRAL,
    Business,
    DIANExtractionJob,
    PaymentRecord,
    Subscription,
    TelegramLinkToken,
    User,
)
from dian_automation.subscriptions.service import add_months_to_date
from dian_automation.telegram.deep_linking import TelegramDeepLinkingService

logger = logging.getLogger("admin_service")

# Tabla paramétrica de multiplicadores DIAN para cálculo de dígito de verificación
DIAN_DV_WEIGHTS = [71, 67, 59, 53, 47, 43, 41, 37, 29, 23, 19, 17, 13, 7, 3]

PLANS_CONFIG: Dict[str, Dict[str, Any]] = {
    "TRIMESTRAL": {
        "months": 3,
        "days": 90,
        "discount_rate": 5.00,
        "base_price": 150000.0,
        "final_price": 142500.0,
    },
    "SEMESTRAL": {
        "months": 6,
        "days": 180,
        "discount_rate": 8.00,
        "base_price": 300000.0,
        "final_price": 276000.0,
    },
    "ANUAL": {
        "months": 12,
        "days": 365,
        "discount_rate": 10.00,
        "base_price": 600000.0,
        "final_price": 540000.0,
    },
}

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

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


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


@dataclass
class PaymentResult:
    """Resultado tipado de la confirmación de pago y reactivación."""
    payment: PaymentRecord
    subscription: Subscription
    client: User
    business: Business
    new_cutoff_date: date
    client_notified: bool


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
) -> CreatedClient:
    """Crea o reactiva un cliente (persona o empresa), su negocio y suscripción, generando enlace mágico."""
    plan_clean = (data.plan or "").upper().strip()
    if plan_clean not in PLANS_CONFIG:
        valid_plans = ", ".join(PLANS_CONFIG.keys())
        raise AdminServiceError("INVALID_PLAN", f"Plan '{data.plan}' no reconocido. Opciones válidas: {valid_plans}", 400)
    plan_info = PLANS_CONFIG[plan_clean]

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

        # Crear o renovar Suscripción con Descuento
        today = date.today()
        cutoff = today + timedelta(days=plan_info["days"])
        grace_end = cutoff + timedelta(days=3)

        subscription = db.query(Subscription).filter(Subscription.client_id == user.id).first()
        if not subscription:
            subscription = Subscription(
                client_id=user.id,
                plan=plan_clean,
                discount_rate=plan_info["discount_rate"],
                base_price=plan_info["base_price"],
                final_price=plan_info["final_price"],
                start_date=today,
                cutoff_date=cutoff,
                grace_period_end=grace_end,
                status="ACTIVO",
            )
            db.add(subscription)
        else:
            subscription.plan = plan_clean
            subscription.discount_rate = plan_info["discount_rate"]
            subscription.base_price = plan_info["base_price"]
            subscription.final_price = plan_info["final_price"]
            subscription.cutoff_date = cutoff
            subscription.grace_period_end = grace_end
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
            final_price=float(plan_info["final_price"]),
            discount_rate=float(plan_info["discount_rate"]),
            cutoff_date=cutoff,
            grace_period_end=grace_end,
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

    try:
        admin_id = admin.id if admin else None
        payment = PaymentRecord(
            subscription_id=sub.id,
            amount=amount_val,
            payment_date=date.today(),
            payment_method="TRANSFERENCIA",
            reference_code=ref_code,
            verified_by_admin_id=admin_id,
            notes=f"Pago confirmado por admin {admin.full_name if admin else admin_id}",
        )
        db.add(payment)

        today = date.today()
        plan_cfg = PLANS_CONFIG.get(sub.plan, {"months": 3})
        months_to_add = plan_cfg.get("months", 3)

        base_renewal_date = sub.cutoff_date if sub.cutoff_date >= today else today
        new_cutoff = add_months_to_date(base_renewal_date, months_to_add)
        new_grace_end = new_cutoff + timedelta(days=3)

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
