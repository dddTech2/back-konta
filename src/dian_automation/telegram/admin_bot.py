"""Bot de Telegram para la Administradora Comercial (Katerinn).

Permite dar de alta clientes en segundos mediante plantillas (/crear_cliente),
asociar su empresa y suscripción con descuentos (Trimestral -5%, Semestral -8%, Anual -10%),
y generar automáticamente el enlace de invitación Deep Linking (/start <token>).
"""

import re
import logging
import calendar
import unicodedata
from decimal import Decimal
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, List, Callable
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session

from dian_automation.branding import BRAND_NAME
from dian_automation.config import config
from dian_automation.core import admin_service, document_service
from dian_automation.core.admin_service import AdminServiceError, NewClientData
from dian_automation.db.models import (
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    IVA_PERIODICITY_BIMESTRAL,
    IVA_PERIODICITY_CUATRIMESTRAL,
    User,
    Business,
    Subscription,
    PaymentRecord,
)
from dian_automation.subscriptions.service import add_months_to_date
from dian_automation.telegram.deep_linking import TelegramDeepLinkingService

logger = logging.getLogger("admin_bot")


class AdminTelegramBot:
    """Procesador de comandos comerciales para administradores autorizados."""

    # Tabla paramétrica de multiplicadores DIAN para cálculo de dígito de verificación
    DIAN_DV_WEIGHTS = admin_service.DIAN_DV_WEIGHTS
    PLANS_CONFIG: Dict[str, Dict[str, Any]] = admin_service.PLANS_CONFIG

    @classmethod
    def calculate_dian_dv(cls, nit: str) -> str:
        """Calcula el dígito de verificación oficial de la DIAN mediante algoritmo módulo 11."""
        return admin_service.calculate_dian_dv(nit)

    @classmethod
    def is_authorized_admin(cls, sender_chat_id: int, db: Session) -> bool:
        """Valida si el remitente posee rol ADMIN y se encuentra activo en el sistema."""
        admin = (
            db.query(User)
            .filter(
                User.telegram_chat_id == sender_chat_id,
                User.role == "ADMIN",
                User.is_active.is_(True),
            )
            .first()
        )
        return admin is not None

    USAGE_MESSAGE = (
        "⚠️ *Uso incorrecto.* La plantilla depende del *Tipo de cliente* (primer campo):\n\n"
        "👤 *Persona Natural:*\n"
        "`/crear_cliente PERSONA | Nombre Completo | Celular | Cédula | Plan [| Módulo]`\n"
        "_Ejemplo (DIAN):_ `/crear_cliente PERSONA | Andrea Torres | 3001234567 | 1000000001 | TRIMESTRAL`\n"
        "_Ejemplo (Ventas):_ `/crear_cliente PERSONA | Andrea Torres | 3001234567 | 1000000001 | TRIMESTRAL | VENTAS_MANUALES`\n\n"
        "🏢 *Empresa (Representante Legal):*\n"
        "`/crear_cliente EMPRESA | Nombre Contacto | Celular | Nombre Empresa | NIT Empresa | Cédula Representante | Plan [| Módulo]`\n"
        "_Ejemplo (DIAN):_ `/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Ferretería El Roble SAS | 901008579 | 10000002 | TRIMESTRAL`\n"
        "_Ejemplo (Ventas):_ `/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Ferretería El Roble SAS | 901008579 | 10000002 | TRIMESTRAL | VENTAS_MANUALES`\n\n"
        "_Planes disponibles: TRIMESTRAL, SEMESTRAL, ANUAL_\n"
        "*Módulo* (último campo, opcional):\n"
        "• `FACTURADOR` (por defecto): factura electrónicamente, cifras e IVA desde la DIAN.\n"
        "• `VENTAS_MANUALES`: registra sus ventas a mano en el módulo de ingresos, sin IVA ni calendario tributario."
    )

    # Tipo de negocio que la administradora escribe en los comandos -> valor guardado en `businesses.income_source`.
    INCOME_SOURCE_BY_TIPO: Dict[str, str] = {
        "FACTURADOR": INCOME_SOURCE_DIAN,
        "VENTAS_MANUALES": INCOME_SOURCE_MANUAL_SALES,
    }
    INCOME_SOURCE_LABELS: Dict[str, str] = {
        INCOME_SOURCE_DIAN: "🧾 Facturador electrónico (cifras desde la DIAN)",
        INCOME_SOURCE_MANUAL_SALES: "✍️ Ventas manuales (registra sus ventas a mano)",
    }

    # Tipos de documentos y alias aceptados por el bot (Story 7.4a)
    DOC_TYPE_ALIASES: Dict[str, str] = {
        "RUT": document_service.DOC_TYPE_RUT,
        "CAMARA": document_service.DOC_TYPE_CAMARA_COMERCIO,
        "CAMARA_COMERCIO": document_service.DOC_TYPE_CAMARA_COMERCIO,
        "CEDULA": document_service.DOC_TYPE_CEDULA_REPRESENTANTE,
        "CEDULA_REPRESENTANTE": document_service.DOC_TYPE_CEDULA_REPRESENTANTE,
        "BANCARIA": document_service.DOC_TYPE_CERTIFICACION_BANCARIA,
        "CERTIFICACION_BANCARIA": document_service.DOC_TYPE_CERTIFICACION_BANCARIA,
        "OTRO": document_service.DOC_TYPE_OTRO,
    }

    SUBIR_DOCUMENTO_USAGE = (
        "📄 *Uso de /subir_documento:*\n"
        "Envía el archivo (PDF, PNG o JPG) con el siguiente mensaje adjunto (caption):\n"
        "`/subir_documento <NIT> <TIPO> [descripción]`\n\n"
        "*Tipos disponibles:* `RUT`, `CAMARA` (o `CAMARA_COMERCIO`), `CEDULA`, `BANCARIA` (o `CERTIFICACION_BANCARIA`), `OTRO`.\n"
        "_Nota:_ Si usas `OTRO`, la descripción es obligatoria.\n\n"
        "_Ejemplo:_ `/subir_documento 901008579 RUT`\n"
        "_Ejemplo con descripción:_ `/subir_documento 901008579 OTRO Contrato de arrendamiento`"
    )

    DOCUMENTOS_USAGE = (
        "📄 *Uso de /documentos:*\n"
        "`/documentos <NIT>`\n\n"
        "_Ejemplo:_ `/documentos 901008579`"
    )

    RETIRAR_DOCUMENTO_USAGE = (
        "🗑️ *Uso de /retirar_documento:*\n"
        "`/retirar_documento <NIT> <número>`\n\n"
        "_Ejemplo:_ `/retirar_documento 901008579 1`"
    )

    @classmethod
    def _format_doc_size(cls, size_bytes: int) -> str:
        """Formatea el tamaño de un archivo en B, KB o MB."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"

    @classmethod
    def _format_doc_date(cls, dt: datetime) -> str:
        """Formatea la fecha de creación en hora de Bogotá (UTC-5)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        bogota_dt = dt.astimezone(ZoneInfo("America/Bogota"))
        return bogota_dt.strftime("%d/%m/%Y %H:%M")


    @staticmethod
    def _plain_upper(raw: str) -> str:
        """Mayúsculas sin acentos (RETENCIÓN, SÍ), también cuando el acento llega como marca combinada."""
        decomposed = unicodedata.normalize("NFKD", raw.strip())
        return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).upper()

    @classmethod
    def normalize_nit(cls, raw: str) -> Optional[str]:
        """NIT solo con dígitos; admite `-DV` final (901008579-7). None si tiene menos de 6 dígitos."""
        return admin_service.normalize_nit(raw)

    @classmethod
    def parse_crear_cliente_command(cls, text: str) -> Tuple[bool, Optional[Dict[str, str]], Optional[str]]:
        """Analiza la sintaxis del comando /crear_cliente, condicionada al Tipo (PERSONA o EMPRESA)."""
        # Eliminar el comando inicial
        match = re.match(r"^/crear_cliente\b\s*(.*)", text, re.IGNORECASE | re.DOTALL)
        if not match:
            return False, None, "Comando no reconocido."

        payload = match.group(1).strip()
        if not payload:
            return False, None, cls.USAGE_MESSAGE

        parts = [p.strip() for p in payload.split("|")]
        if len(parts) < 2:
            return False, None, cls.USAGE_MESSAGE

        tipo_raw = parts[0]
        tipo_clean = tipo_raw.upper()
        if tipo_clean not in ("PERSONA", "EMPRESA"):
            return (
                False,
                None,
                f"⚠️ Tipo de cliente '{tipo_raw}' no reconocido. Debe ser `PERSONA` o `EMPRESA`.\n\n"
                + cls.USAGE_MESSAGE,
            )

        max_parts = 6 if tipo_clean == "PERSONA" else 8  # el TIPO de negocio (opcional) va al final
        if len(parts) > max_parts:
            return False, None, cls.USAGE_MESSAGE

        if tipo_clean == "PERSONA":
            if len(parts) < 5:
                return (
                    False,
                    None,
                    "⚠️ *Faltan datos en la plantilla de Persona Natural.* Debes incluir 5 campos separados por `|`:\n"
                    "`/crear_cliente PERSONA | Nombre | Celular | Cédula | Plan`\n\n"
                    "_Ejemplo:_ `/crear_cliente PERSONA | Andrea Torres | 3001234567 | 1000000001 | TRIMESTRAL`",
                )
            name, phone, doc_raw, plan_raw = parts[1], parts[2], parts[3], parts[4]
            business_name_raw = None
            rep_doc_raw = None
        else:
            if len(parts) < 7:
                return (
                    False,
                    None,
                    "⚠️ *Faltan datos en la plantilla de Empresa.* Debes incluir 7 campos separados por `|`:\n"
                    "`/crear_cliente EMPRESA | Nombre Contacto | Celular | Nombre Empresa | NIT Empresa | "
                    "Cédula Representante | Plan`\n\n"
                    "_Ejemplo:_ `/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Ferretería El Roble SAS | "
                    "901008579 | 10000002 | TRIMESTRAL`",
                )
            name, phone, business_name_raw, doc_raw, rep_doc_raw, plan_raw = (
                parts[1], parts[2], parts[3], parts[4], parts[5], parts[6],
            )

        if (
            not name
            or not phone
            or not doc_raw
            or not plan_raw
            or (tipo_clean == "EMPRESA" and (not rep_doc_raw or not business_name_raw))
        ):
            return False, None, "⚠️ Ningún campo de la plantilla puede estar vacío."

        nit_clean = "".join(filter(str.isdigit, doc_raw))
        if len(nit_clean) < 6:
            campo = "NIT de la empresa" if tipo_clean == "EMPRESA" else "Cédula"
            return False, None, f"⚠️ El {campo} '{doc_raw}' no es válido (debe tener al menos 6 dígitos numéricos)."

        rep_doc_clean = None
        if tipo_clean == "EMPRESA":
            rep_doc_clean = "".join(filter(str.isdigit, rep_doc_raw))
            if len(rep_doc_clean) < 6:
                return (
                    False,
                    None,
                    f"⚠️ La Cédula del Representante Legal '{rep_doc_raw}' no es válida "
                    "(debe tener al menos 6 dígitos numéricos).",
                )

        plan_clean = plan_raw.upper()
        if plan_clean not in cls.PLANS_CONFIG:
            planes_validos = ", ".join(cls.PLANS_CONFIG.keys())
            return False, None, f"⚠️ Plan '{plan_raw}' no reconocido. Opciones válidas: {planes_validos}"

        income_source = None  # sin TIPO: un negocio nuevo queda DIAN y uno existente conserva el suyo
        negocio_pos = 5 if tipo_clean == "PERSONA" else 7
        negocio_raw = parts[negocio_pos] if len(parts) > negocio_pos else ""
        if negocio_raw:
            income_source = cls.INCOME_SOURCE_BY_TIPO.get(negocio_raw.upper())
            if income_source is None:
                return (
                    False,
                    None,
                    f"⚠️ Módulo '{negocio_raw}' no reconocido. Debe ser `FACTURADOR` o `VENTAS_MANUALES`.\n\n"
                    + cls.USAGE_MESSAGE,
                )

        return True, {
            "tipo_cliente": tipo_clean,
            "full_name": name,
            "phone": phone,
            "business_name": business_name_raw.strip() if business_name_raw else None,
            "nit": nit_clean,
            "legal_rep_doc": rep_doc_clean,
            "plan": plan_clean,
            "income_source": income_source,
        }, None

    @classmethod
    def execute_crear_cliente(
        cls,
        sender_chat_id: int,
        text: str,
        db: Session,
        bot_username: str = "KontaBot",
    ) -> Dict[str, Any]:
        """Flujo completo de alta de cliente invocado por la administradora comercial."""
        # 1. Autorización
        if not cls.is_authorized_admin(sender_chat_id, db):
            logger.warning(f"Intento de acceso administrativo no autorizado desde chat_id={sender_chat_id}")
            return {
                "success": False,
                "reason": "UNAUTHORIZED",
                "message": "⛔ *Acceso denegado:* Este comando está restringido a la administración comercial autorizada.",
            }

        # 2. Parsing de la plantilla
        is_valid, data, error_msg = cls.parse_crear_cliente_command(text)
        if not is_valid:
            return {
                "success": False,
                "reason": "INVALID_SYNTAX",
                "message": error_msg,
            }

        admin_user = (
            db.query(User)
            .filter(User.telegram_chat_id == sender_chat_id, User.role == "ADMIN", User.is_active.is_(True))
            .first()
        )

        client_data = NewClientData(
            tipo_cliente=data["tipo_cliente"],
            full_name=data["full_name"],
            phone=data["phone"],
            business_name=data["business_name"],
            nit=data["nit"],
            legal_rep_doc=data["legal_rep_doc"],
            plan=data["plan"],
            income_source=data["income_source"],
        )

        try:
            created = admin_service.create_client(
                db=db,
                admin=admin_user,
                data=client_data,
                bot_username=bot_username,
            )
        except AdminServiceError as e:
            return {
                "success": False,
                "reason": e.code,
                "message": f"❌ {e.message}",
            }
        except Exception as e:
            db.rollback()
            logger.error(f"Error registrando cliente: {e}", exc_info=True)
            return {
                "success": False,
                "reason": "INTERNAL_ERROR",
                "message": f"❌ Error interno al crear el cliente: {str(e)}",
            }

        user = created.user
        business = created.business
        subscription = created.subscription
        tipo_cliente = created.tipo_cliente
        nit_clean = business.nit
        dv = business.dv
        legal_rep_doc = business.legal_rep_doc
        plan_name = subscription.plan
        plan_info = cls.PLANS_CONFIG[plan_name]
        cutoff = subscription.cutoff_date
        grace_end = subscription.grace_period_end
        deep_link_url = created.deep_link_url

        # Construir respuesta enriquecida para Katerinn
        tipo_label = "🏢 Empresa (Representante Legal)" if tipo_cliente == "EMPRESA" else "👤 Persona Natural"
        dv_note = "_(DV calculado automáticamente — algoritmo DIAN módulo 11, no fue suministrado)_"
        if tipo_cliente == "EMPRESA":
            id_lines = (
                f"🏢 *Empresa (razón social):* {business.legal_name}\n"
                f"🆔 *NIT Empresa:* `{nit_clean}-{dv}` {dv_note}\n"
                f"🪪 *Cédula Representante:* `{legal_rep_doc}`\n"
                f"👤 *Contacto:* {user.full_name}\n"
            )
        else:
            id_lines = f"🪪 *Cédula:* `{nit_clean}-{dv}` {dv_note}\n"

        cliente_line = f"👤 *Cliente:* {user.full_name}\n" if tipo_cliente == "PERSONA" else ""

        response_message = (
            "✅ *Cliente creado con éxito.*\n\n"
            f"📂 *Tipo:* {tipo_label}\n"
            f"🗂️ *Tipo de negocio:* {cls.INCOME_SOURCE_LABELS.get(business.income_source, business.income_source)}\n"
            f"{cliente_line}"
            f"📱 *Teléfono:* `{user.phone}`\n"
            f"{id_lines}"
            f"📋 *Plan:* {plan_name} (Descuento {plan_info['discount_rate']:.0f}%)\n"
            f"💰 *Total a Cobrar:* ${plan_info['final_price']:,.0f} COP\n"
            f"📅 *Próximo Corte:* {cutoff.strftime('%d/%m/%Y')} (Gracia hasta {grace_end.strftime('%d/%m/%Y')})\n\n"
            "📲 *Reenvíale este enlace para vincular su Telegram:*\n"
            f"`{deep_link_url}`\n\n"
            "ℹ️ _El enlace es de un solo uso y expira en 72 horas._"
        )

        logger.info(
            f"Admin chat {sender_chat_id} creó cliente {tipo_cliente} '{user.full_name}' con NIT {nit_clean}-{dv}"
            + (f" y representante {legal_rep_doc}" if legal_rep_doc else "")
        )

        return {
            "success": True,
            "user_id": user.id,
            "business_id": business.id,
            "tipo_cliente": tipo_cliente,
            "nit": f"{nit_clean}-{dv}",
            "legal_rep_doc": legal_rep_doc,
            "income_source": business.income_source,
            "plan": plan_name,
            "final_price": plan_info["final_price"],
            "deep_link_url": deep_link_url,
            "message": response_message,
        }

    @classmethod
    def list_clients_summary(cls, sender_chat_id: int, db: Session, limit: int = 10) -> Dict[str, Any]:
        """Lista los clientes registrados y su estado de vinculación para la administradora."""
        if not cls.is_authorized_admin(sender_chat_id, db):
            return {
                "success": False,
                "message": "⛔ Acceso denegado.",
            }

        admin_user = (
            db.query(User)
            .filter(User.telegram_chat_id == sender_chat_id, User.role == "ADMIN", User.is_active.is_(True))
            .first()
        )
        users = admin_service.list_clients(db=db, admin=admin_user, limit=limit)

        if not users:
            return {
                "success": True,
                "message": "ℹ️ No hay clientes registrados actualmente en la plataforma.",
            }

        lines = ["👥 *CLIENTES REGISTRADOS:*", ""]
        for u in users:
            biz = u.businesses[0] if u.businesses else None
            nit_str = f"{biz.nit}-{biz.dv}" if biz else "Sin NIT"
            status_emoji = "🟢" if u.is_telegram_linked else "🟡"
            link_desc = "Vinculado" if u.is_telegram_linked else "Pendiente Telegram"
            sub = u.subscriptions[0] if u.subscriptions else None
            plan_str = sub.plan if sub else "Sin Plan"

            lines.append(f"{status_emoji} *{u.full_name}* | `{nit_str}`")
            lines.append(f"   Plan: {plan_str} | Estado: {link_desc}")

        return {
            "success": True,
            "message": "\n".join(lines),
        }

    @classmethod
    def parse_confirmar_pago_command(cls, text: str) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Analiza la sintaxis del comando /confirmar_pago NIT | Monto | Referencia."""
        match = re.match(r"^/confirmar_pago\b\s*(.*)", text, re.IGNORECASE | re.DOTALL)
        if not match:
            return False, None, "Comando no reconocido."

        payload = match.group(1).strip()
        if not payload:
            return (
                False,
                None,
                "⚠️ *Uso incorrecto.* Formato requerido:\n"
                "`/confirmar_pago NIT | Monto | Referencia`\n\n"
                "_Ejemplo:_ `/confirmar_pago 901008579 | 142500 | TR-998822`\n"
                "_Ejemplo con DV:_ `/confirmar_pago 901008579-7 | 142500 | NEQUI-4411`",
            )

        parts = [p.strip() for p in payload.split("|")]
        if len(parts) < 3:
            return (
                False,
                None,
                "⚠️ *Faltan datos en la plantilla.* Debes incluir los 3 campos separados por `|`:\n"
                "`/confirmar_pago NIT | Monto | Referencia`\n\n"
                "_Ejemplo:_ `/confirmar_pago 901008579 | 142500 | TR-998822`",
            )

        nit_raw, amount_raw, ref_code = parts[0], parts[1], parts[2]
        if not nit_raw or not amount_raw or not ref_code:
            return False, None, "⚠️ Ningún campo de la plantilla puede estar vacío."

        # Limpiar NIT (admitir 901008579 o 901008579-7)
        if "-" in nit_raw:
            nit_main = nit_raw.split("-")[0]
        else:
            nit_main = nit_raw
        nit_clean = "".join(filter(str.isdigit, nit_main))
        if len(nit_clean) < 6:
            return False, None, f"⚠️ El NIT '{nit_raw}' no es válido."

        # Parsear monto
        amount_clean_str = re.sub(r"[^\d.]", "", amount_raw)
        try:
            amount_val = Decimal(amount_clean_str)
            if amount_val <= 0:
                return False, None, "⚠️ El monto del pago debe ser mayor a cero."
        except Exception:
            return False, None, f"⚠️ Monto '{amount_raw}' inválido. Ingresa un valor numérico."

        return True, {
            "nit": nit_clean,
            "amount": amount_val,
            "reference_code": ref_code,
        }, None

    @classmethod
    def execute_confirmar_pago(
        cls,
        sender_chat_id: int,
        text: str,
        db: Session,
        telegram_sender: Optional[Callable[[int, str], bool]] = None,
    ) -> Dict[str, Any]:
        """Flujo completo de confirmación de pago, registro contable y reactivación inmediata."""
        # 1. Autorización
        if not cls.is_authorized_admin(sender_chat_id, db):
            return {
                "success": False,
                "reason": "UNAUTHORIZED",
                "message": "⛔ *Acceso denegado:* Este comando está restringido a la administración comercial autorizada.",
            }

        admin_user = (
            db.query(User)
            .filter(User.telegram_chat_id == sender_chat_id, User.role == "ADMIN")
            .first()
        )

        # 2. Parsing de la plantilla
        is_valid, data, error_msg = cls.parse_confirmar_pago_command(text)
        if not is_valid:
            return {
                "success": False,
                "reason": "INVALID_SYNTAX",
                "message": error_msg,
            }

        nit_clean = data["nit"]
        amount = data["amount"]
        ref_code = data["reference_code"]

        # 3. Localizar negocio y cliente
        try:
            result = admin_service.confirm_payment(
                db=db,
                admin=admin_user,
                nit=nit_clean,
                amount=amount,
                reference=ref_code,
                notifier=telegram_sender,
            )
        except AdminServiceError as e:
            if e.code == "BUSINESS_NOT_FOUND":
                msg = f"❌ No se encontró ningún negocio registrado con NIT `{nit_clean}`."
            elif e.code == "CLIENT_NOT_FOUND":
                biz = db.query(Business).filter(Business.nit == nit_clean).first()
                biz_name = biz.commercial_name if biz else ""
                msg = f"❌ No existe usuario cliente asociado al negocio '{biz_name}'."
            elif e.code == "SUBSCRIPTION_NOT_FOUND":
                biz = db.query(Business).filter(Business.nit == nit_clean).first()
                client = biz.client if biz else None
                client_name = client.full_name if client else ""
                msg = f"❌ El cliente {client_name} no tiene ninguna suscripción registrada."
            else:
                msg = f"❌ {e.message}"
            return {
                "success": False,
                "reason": e.code,
                "message": msg,
            }
        except Exception as e:
            db.rollback()
            logger.error(f"Error registrando pago para NIT {nit_clean}: {e}", exc_info=True)
            return {
                "success": False,
                "reason": "INTERNAL_ERROR",
                "message": f"❌ Error interno al registrar el pago: {str(e)}",
            }

        client = result.client
        biz = result.business
        sub = result.subscription
        payment = result.payment
        client_notified = result.client_notified

        notif_note = (
            "📲 _Se ha notificado al cliente automáticamente en Telegram._"
            if client_notified
            else "⚠️ _El cliente no tiene Telegram vinculado todavía._"
        )

        admin_response_msg = (
            "✅ *Pago registrado y suscripción reactivada exitosamente.*\n\n"
            f"👤 *Cliente:* {client.full_name}\n"
            f"🏢 *Empresa:* {biz.commercial_name} (NIT `{biz.nit}-{biz.dv}`)\n"
            f"💰 *Monto Abonado:* ${amount:,.0f} COP\n"
            f"🔖 *Referencia:* `{ref_code}`\n"
            f"📋 *Plan:* {sub.plan}\n"
            f"📅 *Nuevo Próximo Corte:* {sub.cutoff_date.strftime('%d/%m/%Y')}\n"
            f"⏳ *Nuevo Periodo de Gracia:* {sub.grace_period_end.strftime('%d/%m/%Y')}\n"
            "🟢 *Estado Actual:* ACTIVO\n\n"
            f"{notif_note}"
        )

        logger.info(
            f"Admin chat {sender_chat_id} confirmó pago de ${amount:,.0f} para NIT {biz.nit}-{biz.dv}. "
            f"Suscripción {sub.id} reactivada hasta {sub.cutoff_date}."
        )

        return {
            "success": True,
            "payment_id": payment.id,
            "subscription_id": sub.id,
            "client_id": client.id,
            "new_cutoff_date": sub.cutoff_date.isoformat(),
            "status": "ACTIVO",
            "client_notified": client_notified,
            "message": admin_response_msg,
        }

    @classmethod
    def resolve_months_range(cls, n_months: int, reference_date: Optional[date] = None) -> str:
        """Calcula un único rango cubriendo los últimos N meses calendario COMPLETOS."""
        return admin_service.resolve_months_range(n_months, reference_date)

    @classmethod
    def parse_ejecutar_extraccion_command(cls, text: str) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Analiza la sintaxis del comando /ejecutar_extraccion NIT | [Periodo]."""
        match = re.match(r"^/ejecutar_extraccion\b\s*(.*)", text, re.IGNORECASE | re.DOTALL)
        if not match:
            return False, None, "Comando no reconocido."

        payload = match.group(1).strip()
        if not payload:
            return (
                False,
                None,
                "⚠️ *Uso incorrecto.* Formato requerido:\n"
                "`/ejecutar_extraccion NIT | Periodo`\n\n"
                "_Ejemplo (un mes):_ `/ejecutar_extraccion 901008579 | 2026-08`\n"
                "_Ejemplo (rango de meses):_ `/ejecutar_extraccion 901008579 | 6 meses`\n\n"
                "_El período es opcional. Formatos válidos: `YYYY-MM` para un mes exacto, o_ "
                "`N meses`_ para los últimos N meses completos en un solo rango. Si se omite, "
                "se usa el mes calendario anterior completo._",
            )

        parts = [p.strip() for p in payload.split("|")]
        nit_raw = parts[0]
        period_raw = parts[1] if len(parts) > 1 and parts[1].strip() else None

        if not nit_raw:
            return False, None, "⚠️ Debes indicar el NIT del negocio."

        # Admitir NIT con o sin DV (901008579 o 901008579-7)
        if "-" in nit_raw:
            possible_dv = nit_raw.rsplit("-", 1)[-1].strip()
            if len(possible_dv) == 1 and possible_dv.isdigit():
                nit_raw = nit_raw.rsplit("-", 1)[0]
        nit_clean = "".join(filter(str.isdigit, nit_raw))
        if len(nit_clean) < 6:
            return False, None, f"⚠️ El NIT '{nit_raw}' no es válido (debe tener al menos 6 dígitos numéricos)."

        period_clean = None
        if period_raw:
            months_match = re.match(r"^(\d{1,2})\s*mes(?:es)?$", period_raw, re.IGNORECASE)
            if months_match:
                n_months = int(months_match.group(1))
                if not (1 <= n_months <= 24):
                    return (
                        False,
                        None,
                        f"⚠️ '{period_raw}' no es válido: el rango debe ser entre 1 y 24 meses.",
                    )
                period_clean = cls.resolve_months_range(n_months)
            elif re.match(r"^\d{4}-\d{2}$", period_raw):
                period_clean = period_raw
            else:
                return (
                    False,
                    None,
                    f"⚠️ El período '{period_raw}' no es válido. Usa `YYYY-MM` (ej. `2026-08`) para un "
                    "mes exacto, o `N meses` (ej. `6 meses`) para un rango de los últimos N meses completos.",
                )

        return True, {"nit": nit_clean, "period": period_clean}, None

    @classmethod
    def execute_ejecutar_extraccion(
        cls,
        sender_chat_id: int,
        text: str,
        db: Session,
    ) -> Dict[str, Any]:
        """Encola una extracción DIAN real para el negocio indicado por NIT.

        Solo AGREGA el trabajo a la cola (DIANExtractionJob). Para que se procese de
        verdad contra el portal DIAN, el proceso `kontable-worker` debe estar corriendo
        por separado (ver mensaje de respuesta / README).
        """
        if not cls.is_authorized_admin(sender_chat_id, db):
            return {
                "success": False,
                "reason": "UNAUTHORIZED",
                "message": "⛔ *Acceso denegado:* Este comando está restringido a la administración comercial autorizada.",
            }

        is_valid, data, error_msg = cls.parse_ejecutar_extraccion_command(text)
        if not is_valid:
            return {
                "success": False,
                "reason": "INVALID_SYNTAX",
                "message": error_msg,
            }

        admin_user = (
            db.query(User)
            .filter(User.telegram_chat_id == sender_chat_id, User.role == "ADMIN")
            .first()
        )

        nit_clean = data["nit"]
        period = data["period"]

        try:
            job = admin_service.enqueue_extraction(
                db=db,
                admin=admin_user,
                nit=nit_clean,
                period_spec=period,
            )
        except AdminServiceError as e:
            if e.code == "BUSINESS_NOT_FOUND":
                msg = f"❌ No se encontró ningún negocio registrado con NIT `{nit_clean}`."
            else:
                msg = f"❌ {e.message}"
            return {
                "success": False,
                "reason": e.code,
                "message": msg,
            }
        except Exception as e:
            logger.error(f"Error encolando extracción para NIT {nit_clean}: {e}", exc_info=True)
            return {
                "success": False,
                "reason": "INTERNAL_ERROR",
                "message": f"❌ Error interno al encolar la extracción: {str(e)}",
            }

        biz = job.business or db.query(Business).filter(Business.id == job.business_id).first()
        actual_period = job.target_period
        modo = "Empresa (Representante Legal)" if biz.taxpayer_type == "PERSONA_JURIDICA" else "Persona Natural"
        periodo_label = "Rango de meses" if " - " in actual_period else "Mes"
        message = (
            "📥 *Extracción DIAN encolada.*\n\n"
            f"🏢 *Empresa:* {biz.commercial_name} (NIT `{biz.nit}-{biz.dv}`)\n"
            f"📂 *Modo de login:* {modo}\n"
            f"📅 *{periodo_label} objetivo:* `{actual_period}`\n"
            f"🆔 *Job:* `{job.id}`\n"
            f"⏳ *Estado:* {job.status}\n\n"
            "El trabajo se procesa automáticamente; recibirás un aviso si algo falla."
        )

        logger.info(f"Admin chat {sender_chat_id} encoló extracción para NIT {nit_clean} periodo {actual_period} (job {job.id})")

        return {
            "success": True,
            "job_id": job.id,
            "business_id": biz.id,
            "period": actual_period,
            "message": message,
        }

    CAMBIAR_TIPO_USAGE = (
        "⚠️ *Uso incorrecto.* Formato requerido:\n"
        "`/cambiar_tipo NIT | TIPO`\n\n"
        "_TIPO:_ `FACTURADOR` (factura electrónicamente, cifras desde la DIAN) o `VENTAS_MANUALES` "
        "(registra sus ventas a mano).\n"
        "_Ejemplo:_ `/cambiar_tipo 901008579 | VENTAS_MANUALES`"
    )

    @classmethod
    def parse_cambiar_tipo_command(cls, text: str) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Analiza `/cambiar_tipo NIT | FACTURADOR|VENTAS_MANUALES`."""
        match = re.match(r"^/cambiar_tipo\b\s*(.*)", text, re.IGNORECASE | re.DOTALL)
        if not match:
            return False, None, "Comando no reconocido."

        parts = [p.strip() for p in match.group(1).strip().split("|")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return False, None, cls.CAMBIAR_TIPO_USAGE

        nit = cls.normalize_nit(parts[0])
        if not nit:
            return (
                False,
                None,
                f"⚠️ El NIT '{parts[0]}' no es válido (debe tener al menos 6 dígitos numéricos).\n\n"
                + cls.CAMBIAR_TIPO_USAGE,
            )

        income_source = cls.INCOME_SOURCE_BY_TIPO.get(parts[1].upper())
        if income_source is None:
            return False, None, f"⚠️ Tipo '{parts[1]}' no reconocido.\n\n" + cls.CAMBIAR_TIPO_USAGE

        return True, {"nit": nit, "income_source": income_source}, None

    @classmethod
    def execute_cambiar_tipo(cls, sender_chat_id: int, text: str, db: Session) -> Dict[str, Any]:
        """Cambia el tipo (DIAN o ventas manuales) de un negocio existente. No depende de la suscripción."""
        if not cls.is_authorized_admin(sender_chat_id, db):
            logger.warning(f"Intento de /cambiar_tipo no autorizado desde chat_id={sender_chat_id}")
            return {
                "success": False,
                "reason": "UNAUTHORIZED",
                "message": "⛔ *Acceso denegado:* Este comando está restringido a la administración comercial autorizada.",
            }

        is_valid, data, error_msg = cls.parse_cambiar_tipo_command(text)
        if not is_valid:
            return {"success": False, "reason": "INVALID_SYNTAX", "message": error_msg}

        admin_user = (
            db.query(User)
            .filter(User.telegram_chat_id == sender_chat_id, User.role == "ADMIN")
            .first()
        )

        try:
            biz = admin_service.set_income_source(
                db=db,
                admin=admin_user,
                nit=data["nit"],
                income_source=data["income_source"],
            )
        except AdminServiceError as e:
            if e.code == "BUSINESS_NOT_FOUND":
                msg = (
                    f"❌ No se encontró ningún negocio registrado con NIT `{data['nit']}`.\n\n"
                    + cls.CAMBIAR_TIPO_USAGE
                )
            else:
                msg = f"❌ {e.message}"
            return {"success": False, "reason": e.code, "message": msg}
        except Exception as e:
            db.rollback()
            logger.error(f"Error cambiando tipo del NIT {data['nit']}: {e}", exc_info=True)
            return {"success": False, "reason": "INTERNAL_ERROR", "message": f"❌ Error interno al cambiar el tipo: {str(e)}"}

        logger.info(f"Admin chat {sender_chat_id} cambió el tipo del NIT {biz.nit} a {biz.income_source}")
        return {
            "success": True,
            "business_id": biz.id,
            "income_source": biz.income_source,
            "message": (
                "✅ *Tipo de negocio actualizado.*\n\n"
                f"🏢 *Empresa:* {biz.commercial_name} (NIT `{biz.nit}-{biz.dv}`)\n"
                f"🗂️ *Tipo:* {cls.INCOME_SOURCE_LABELS[biz.income_source]}"
            ),
        }

    PERFIL_TRIBUTARIO_USAGE = (
        "⚠️ *Uso incorrecto.* Formato requerido:\n"
        "`/perfil_tributario NIT | IVA=BIMESTRAL|CUATRIMESTRAL|NINGUNO | RETENCION=SI|NO`\n\n"
        "_Ejemplo:_ `/perfil_tributario 901008579 | IVA=CUATRIMESTRAL | RETENCION=SI`\n"
        "_Solo para negocios facturadores; `NINGUNO` = no es responsable de IVA._"
    )

    IVA_BY_VALUE: Dict[str, Optional[str]] = {
        "BIMESTRAL": IVA_PERIODICITY_BIMESTRAL,
        "CUATRIMESTRAL": IVA_PERIODICITY_CUATRIMESTRAL,
        "NINGUNO": None,
    }

    @classmethod
    def parse_perfil_tributario_command(cls, text: str) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Analiza `/perfil_tributario NIT | IVA=... | RETENCION=...` (las dos claves, en cualquier orden)."""
        match = re.match(r"^/perfil_tributario\b\s*(.*)", text, re.IGNORECASE | re.DOTALL)
        if not match:
            return False, None, "Comando no reconocido."

        parts = [p.strip() for p in match.group(1).strip().split("|")]
        if len(parts) != 3 or not parts[0]:
            return False, None, cls.PERFIL_TRIBUTARIO_USAGE

        nit = cls.normalize_nit(parts[0])
        if not nit:
            return (
                False,
                None,
                f"⚠️ El NIT '{parts[0]}' no es válido (debe tener al menos 6 dígitos numéricos).\n\n"
                + cls.PERFIL_TRIBUTARIO_USAGE,
            )

        values: Dict[str, str] = {}
        for part in parts[1:]:
            key, sep, value = part.partition("=")
            key = cls._plain_upper(key)
            if not sep or key not in ("IVA", "RETENCION") or key in values or not value.strip():
                return False, None, cls.PERFIL_TRIBUTARIO_USAGE
            values[key] = cls._plain_upper(value)

        if values["IVA"] not in cls.IVA_BY_VALUE:
            return False, None, f"⚠️ IVA '{values['IVA']}' no reconocido.\n\n" + cls.PERFIL_TRIBUTARIO_USAGE
        if values["RETENCION"] not in ("SI", "NO"):
            return (
                False,
                None,
                f"⚠️ RETENCION '{values['RETENCION']}' no reconocida.\n\n" + cls.PERFIL_TRIBUTARIO_USAGE,
            )

        return True, {
            "nit": nit,
            "iva_periodicity": cls.IVA_BY_VALUE[values["IVA"]],
            "is_withholding_agent": values["RETENCION"] == "SI",
        }, None

    @classmethod
    def execute_perfil_tributario(cls, sender_chat_id: int, text: str, db: Session) -> Dict[str, Any]:
        """Fija la periodicidad de IVA y si es agente de retención de un negocio facturador (DIAN)."""
        if not cls.is_authorized_admin(sender_chat_id, db):
            logger.warning(f"Intento de /perfil_tributario no autorizado desde chat_id={sender_chat_id}")
            return {
                "success": False,
                "reason": "UNAUTHORIZED",
                "message": "⛔ *Acceso denegado:* Este comando está restringido a la administración comercial autorizada.",
            }

        is_valid, data, error_msg = cls.parse_perfil_tributario_command(text)
        if not is_valid:
            return {"success": False, "reason": "INVALID_SYNTAX", "message": error_msg}

        admin_user = (
            db.query(User)
            .filter(User.telegram_chat_id == sender_chat_id, User.role == "ADMIN")
            .first()
        )

        try:
            biz = admin_service.set_tax_profile(
                db=db,
                admin=admin_user,
                nit=data["nit"],
                iva_periodicity=data["iva_periodicity"],
                is_withholding_agent=data["is_withholding_agent"],
            )
        except AdminServiceError as e:
            if e.code == "BUSINESS_NOT_FOUND":
                msg = (
                    f"❌ No se encontró ningún negocio registrado con NIT `{data['nit']}`.\n\n"
                    + cls.PERFIL_TRIBUTARIO_USAGE
                )
            elif e.code == "NOT_APPLICABLE":
                biz_lookup = db.query(Business).filter(Business.nit == data["nit"]).first()
                biz_name = biz_lookup.commercial_name if biz_lookup else ""
                msg = (
                    f"❌ *{biz_name}* registra sus ventas a mano: no tiene IVA, ICA ni otros impuestos "
                    "que configurar. El perfil tributario aplica solo a negocios facturadores "
                    "(`/cambiar_tipo NIT | FACTURADOR`).\n\n" + cls.PERFIL_TRIBUTARIO_USAGE
                )
            else:
                msg = f"❌ {e.message}"
            return {"success": False, "reason": e.code, "message": msg}
        except Exception as e:
            db.rollback()
            logger.error(f"Error guardando perfil tributario del NIT {data['nit']}: {e}", exc_info=True)
            return {"success": False, "reason": "INTERNAL_ERROR", "message": f"❌ Error interno al guardar el perfil: {str(e)}"}

        logger.info(
            f"Admin chat {sender_chat_id} fijó el perfil tributario del NIT {biz.nit}: IVA={biz.iva_periodicity}, "
            f"retención={biz.is_withholding_agent}"
        )
        return {
            "success": True,
            "business_id": biz.id,
            "iva_periodicity": biz.iva_periodicity,
            "is_withholding_agent": biz.is_withholding_agent,
            "message": (
                "✅ *Perfil tributario actualizado.*\n\n"
                f"🏢 *Empresa:* {biz.commercial_name} (NIT `{biz.nit}-{biz.dv}`)\n"
                f"🧮 *IVA:* {biz.iva_periodicity or 'NINGUNO (no responsable de IVA)'}\n"
                f"✂️ *Agente de retención:* {'SÍ' if biz.is_withholding_agent else 'NO'}"
            ),
        }

    LIBERAR_TELEGRAM_USAGE = (
        "⚠️ *Uso incorrecto.* Formato requerido:\n"
        "`/liberar_telegram <chat_id>`\n\n"
        "_Ejemplo:_ `/liberar_telegram 123456789`"
    )

    @classmethod
    def parse_liberar_telegram_command(cls, text: str) -> Tuple[bool, Optional[int], Optional[str]]:
        """Analiza el comando /liberar_telegram <chat_id>."""
        match = re.match(r"^/liberar_telegram\b\s*(.*)", text, re.IGNORECASE | re.DOTALL)
        if not match:
            return False, None, "Comando no reconocido."

        payload = match.group(1).strip()
        if not payload:
            return False, None, cls.LIBERAR_TELEGRAM_USAGE

        clean_val = payload.strip()
        if not clean_val.lstrip("-").isdigit():
            return (
                False,
                None,
                f"⚠️ El Chat ID '{payload}' no es válido. Debe ser un valor numérico.\n\n"
                + cls.LIBERAR_TELEGRAM_USAGE,
            )

        return True, int(clean_val), None

    @classmethod
    def execute_liberar_telegram(cls, sender_chat_id: int, text: str, db: Session) -> Dict[str, Any]:
        """Libera la vinculación de Telegram de un usuario registrado."""
        if not cls.is_authorized_admin(sender_chat_id, db):
            logger.warning(f"Intento de /liberar_telegram no autorizado desde chat_id={sender_chat_id}")
            return {
                "success": False,
                "reason": "UNAUTHORIZED",
                "message": "⛔ *Acceso denegado:* Este comando está restringido a la administración comercial autorizada.",
            }

        is_valid, target_chat_id, error_msg = cls.parse_liberar_telegram_command(text)
        if not is_valid:
            return {"success": False, "reason": "INVALID_SYNTAX", "message": error_msg}

        if target_chat_id == sender_chat_id:
            return {
                "success": False,
                "reason": "CANNOT_FREE_SELF",
                "message": "⚠️ No puedes desvincular tu propia cuenta de administrador.",
            }

        admin_user = (
            db.query(User)
            .filter(User.telegram_chat_id == sender_chat_id, User.role == "ADMIN")
            .first()
        )

        try:
            user = admin_service.release_telegram(
                db=db,
                admin=admin_user,
                chat_id=target_chat_id,
            )
        except AdminServiceError as e:
            if e.code == "CANNOT_FREE_SELF":
                msg = "⚠️ No puedes desvincular tu propia cuenta de administrador."
            elif e.code == "USER_NOT_FOUND":
                msg = "No hay ninguna cuenta vinculada a ese ID."
            else:
                msg = f"❌ {e.message}"
            return {"success": False, "reason": e.code, "message": msg}
        except Exception as e:
            db.rollback()
            logger.error(f"Error liberando Telegram para chat_id={target_chat_id}: {e}", exc_info=True)
            return {
                "success": False,
                "reason": "INTERNAL_ERROR",
                "message": "❌ No pudimos liberar la cuenta. Inténtalo de nuevo; si el problema continúa, contacta a soporte.",
            }

        logger.info(f"Admin chat {sender_chat_id} liberó Telegram de {user.email} (chat_id={target_chat_id})")
        return {
            "success": True,
            "user_id": user.id,
            "user_name": user.full_name,
            "user_email": user.email,
            "message": (
                f"✅ Cuenta liberada: *{user.full_name}* ({user.email}).\n"
                "Ya puede vincular su Telegram con un enlace nuevo."
            ),
        }

    @classmethod
    def handle_admin_document(
        cls,
        sender_chat_id: int,
        message: Dict[str, Any],
        db: Session,
        telegram_sender: Optional[Callable[[int, str], bool]] = None,
        file_downloader: Optional[Callable[[str], bytes]] = None,
    ) -> str:
        """Procesa un archivo (documento o foto) enviado por la administradora comercial."""
        if not cls.is_authorized_admin(sender_chat_id, db):
            return "⛔ *Acceso denegado:* Este comando está restringido a la administración comercial autorizada."

        caption = (message.get("caption") or "").strip()
        if not caption:
            return cls.SUBIR_DOCUMENTO_USAGE

        match = re.match(r"^/subir_documento\b\s*(.*)", caption, re.IGNORECASE | re.DOTALL)
        if not match:
            return cls.SUBIR_DOCUMENTO_USAGE

        payload = match.group(1).strip()
        if not payload:
            return cls.SUBIR_DOCUMENTO_USAGE

        parts = payload.split(maxsplit=2)
        if len(parts) < 2:
            return cls.SUBIR_DOCUMENTO_USAGE

        raw_nit, raw_tipo = parts[0], parts[1]
        raw_desc = parts[2].strip() if len(parts) > 2 else None

        nit = cls.normalize_nit(raw_nit)
        if not nit:
            return (
                f"⚠️ El NIT '{raw_nit}' no es válido (debe tener al menos 6 dígitos numéricos).\n\n"
                + cls.SUBIR_DOCUMENTO_USAGE
            )

        biz = db.query(Business).filter(Business.nit == nit).first()
        if not biz:
            return f"❌ No se encontró ningún negocio registrado con NIT `{nit}`."

        clean_tipo = cls._plain_upper(raw_tipo)
        doc_type = cls.DOC_TYPE_ALIASES.get(clean_tipo)
        if not doc_type:
            return (
                f"⚠️ Tipo de documento '{raw_tipo}' no reconocido.\n\n"
                "Tipos válidos: `RUT`, `CAMARA` (o `CAMARA_COMERCIO`), `CEDULA`, `BANCARIA` (o `CERTIFICACION_BANCARIA`), `OTRO`."
            )

        if doc_type == document_service.DOC_TYPE_OTRO and not raw_desc:
            return "⚠️ Los documentos de tipo 'OTRO' requieren una descripción.\n\n_Ejemplo:_ `/subir_documento 901008579 OTRO Contrato de arrendamiento`"

        if raw_desc and len(raw_desc) > 120:
            return "⚠️ La descripción no puede superar 120 caracteres."

        # Identificar archivo y metadatos reportados por Telegram
        doc = message.get("document")
        photos = message.get("photo")

        if doc:
            file_id = doc.get("file_id")
            file_name = doc.get("file_name") or f"{doc_type}.pdf"
            file_size = doc.get("file_size") or 0
        elif photos and isinstance(photos, list):
            largest_photo = max(photos, key=lambda p: p.get("file_size") or 0)
            file_id = largest_photo.get("file_id")
            file_size = largest_photo.get("file_size") or 0
            today_str = datetime.now().strftime("%Y-%m-%d")
            file_name = f"{doc_type}_{today_str}.jpg"
        else:
            return "⚠️ No se encontró ningún archivo adjunto en el mensaje.\n\n" + cls.SUBIR_DOCUMENTO_USAGE

        if not file_id:
            return "⚠️ No se pudo obtener el identificador del archivo desde Telegram."

        max_bytes = config.document_max_mb * 1024 * 1024
        if file_size and file_size > max_bytes:
            mb_size = file_size / (1024 * 1024)
            return (
                f"❌ El archivo ({mb_size:.1f} MB) supera el tamaño máximo permitido de "
                f"{config.document_max_mb} MB. No se descargó."
            )

        if not file_downloader:
            return "❌ Error interno: descargador de archivos no disponible."

        try:
            content = file_downloader(file_id)
        except Exception as e:
            # Solo el tipo de error: el mensaje o la traza pueden incluir la URL con el token del bot.
            logger.error("Error descargando archivo de Telegram (%s).", type(e).__name__)
            return "❌ Ocurrió un error al descargar el archivo desde Telegram. Inténtalo de nuevo."

        admin_user = db.query(User).filter(User.telegram_chat_id == sender_chat_id).first()
        if not admin_user:
            admin_user = db.query(User).filter(User.role == "ADMIN").first()

        try:
            saved_doc = document_service.save_document(
                db=db,
                business=biz,
                doc_type=doc_type,
                description=raw_desc,
                filename=file_name,
                content=content,
                uploaded_by=admin_user,
            )
        except document_service.DocumentError as de:
            return f"❌ {de.message}"
        except Exception as e:
            logger.error(f"Error guardando documento de negocio NIT {nit}: {e}", exc_info=True)
            return "❌ Error interno guardando el documento."

        doc_number = document_service.get_document_number(db, biz, saved_doc.id)
        doc_label = document_service.DOC_TYPE_LABELS.get(saved_doc.doc_type, saved_doc.doc_type)

        # Notificar al cliente si tiene Telegram vinculado (AC #8)
        owner = db.query(User).filter(User.id == biz.client_id).first()
        if owner and owner.is_telegram_linked and owner.telegram_chat_id and telegram_sender:
            client_msg = f"📄 Katerinn cargó tu {doc_label} en tu panel. Escribe /dashboard para verlo."
            try:
                telegram_sender(owner.telegram_chat_id, client_msg)
            except Exception as e:
                logger.warning(f"Error notificando al cliente sobre documento cargado: {e}")

        num_str = f"#{doc_number}" if doc_number else ""
        reply = (
            "✅ *Documento guardado con éxito*\n\n"
            f"📄 *Tipo:* {doc_label}\n"
            f"🏢 *Negocio:* {biz.commercial_name} (NIT `{biz.nit}-{biz.dv}`)\n"
            f"🔢 *Número:* {num_str}\n"
        )
        if saved_doc.description:
            reply += f"📝 *Descripción:* {saved_doc.description}\n"
        return reply.strip()

    @classmethod
    def parse_documentos_command(cls, text: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """Analiza la sintaxis de /documentos <NIT>."""
        match = re.match(r"^/documentos\b\s*(.*)", text, re.IGNORECASE | re.DOTALL)
        if not match:
            return False, None, "Comando no reconocido."

        payload = match.group(1).strip()
        if not payload:
            return False, None, cls.DOCUMENTOS_USAGE

        nit = cls.normalize_nit(payload)
        if not nit:
            return (
                False,
                None,
                f"⚠️ El NIT '{payload}' no es válido (debe tener al menos 6 dígitos numéricos).\n\n"
                + cls.DOCUMENTOS_USAGE,
            )

        return True, nit, None

    @classmethod
    def execute_documentos(cls, sender_chat_id: int, text: str, db: Session) -> Dict[str, Any]:
        """Consulta y lista los documentos activos de un negocio con numeración estable."""
        if not cls.is_authorized_admin(sender_chat_id, db):
            return {
                "success": False,
                "reason": "UNAUTHORIZED",
                "message": "⛔ *Acceso denegado:* Este comando está restringido a la administración comercial autorizada.",
            }

        is_valid, nit, error_msg = cls.parse_documentos_command(text)
        if not is_valid:
            return {"success": False, "reason": "INVALID_SYNTAX", "message": error_msg}

        biz = db.query(Business).filter(Business.nit == nit).first()
        if not biz:
            return {
                "success": False,
                "reason": "BUSINESS_NOT_FOUND",
                "message": f"❌ No se encontró ningún negocio registrado con NIT `{nit}`.",
            }

        numbered_docs = document_service.list_active_numbered(db, biz)
        if not numbered_docs:
            return {
                "success": True,
                "count": 0,
                "message": f"ℹ️ El negocio *{biz.commercial_name}* (NIT `{biz.nit}-{biz.dv}`) no tiene documentos activos.",
            }

        lines = [
            f"📄 *Documentos activos de {biz.commercial_name}* (NIT `{biz.nit}-{biz.dv}`):",
            "",
        ]
        for num, doc in numbered_docs:
            doc_label = document_service.DOC_TYPE_LABELS.get(doc.doc_type, doc.doc_type)
            date_str = cls._format_doc_date(doc.created_at)
            size_str = cls._format_doc_size(doc.size_bytes)
            if doc.description:
                lines.append(f"{num}. *{doc_label}* — {date_str} — {size_str} — {doc.description}")
            else:
                lines.append(f"{num}. *{doc_label}* — {date_str} — {size_str}")

        lines.append("")
        lines.append(f"Para retirar un documento: `/retirar_documento {biz.nit} <número>`")

        return {
            "success": True,
            "count": len(numbered_docs),
            "message": "\n".join(lines),
        }

    @classmethod
    def parse_retirar_documento_command(cls, text: str) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Analiza la sintaxis de /retirar_documento <NIT> <número>."""
        match = re.match(r"^/retirar_documento\b\s*(.*)", text, re.IGNORECASE | re.DOTALL)
        if not match:
            return False, None, "Comando no reconocido."

        payload = match.group(1).strip()
        if not payload:
            return False, None, cls.RETIRAR_DOCUMENTO_USAGE

        parts = payload.split()
        if len(parts) != 2:
            return False, None, cls.RETIRAR_DOCUMENTO_USAGE

        raw_nit, raw_num = parts[0], parts[1]
        nit = cls.normalize_nit(raw_nit)
        if not nit:
            return (
                False,
                None,
                f"⚠️ El NIT '{raw_nit}' no es válido (debe tener al menos 6 dígitos numéricos).\n\n"
                + cls.RETIRAR_DOCUMENTO_USAGE,
            )

        if not raw_num.isdigit() or int(raw_num) <= 0:
            return (
                False,
                None,
                f"⚠️ El número '{raw_num}' no es válido. Debe ser un entero positivo.\n\n"
                + cls.RETIRAR_DOCUMENTO_USAGE,
            )

        return True, {"nit": nit, "number": int(raw_num)}, None

    @classmethod
    def execute_retirar_documento(cls, sender_chat_id: int, text: str, db: Session) -> Dict[str, Any]:
        """Da de baja un documento por su número estable."""
        if not cls.is_authorized_admin(sender_chat_id, db):
            return {
                "success": False,
                "reason": "UNAUTHORIZED",
                "message": "⛔ *Acceso denegado:* Este comando está restringido a la administración comercial autorizada.",
            }

        is_valid, data, error_msg = cls.parse_retirar_documento_command(text)
        if not is_valid:
            return {"success": False, "reason": "INVALID_SYNTAX", "message": error_msg}

        biz = db.query(Business).filter(Business.nit == data["nit"]).first()
        if not biz:
            return {
                "success": False,
                "reason": "BUSINESS_NOT_FOUND",
                "message": f"❌ No se encontró ningún negocio registrado con NIT `{data['nit']}`.",
            }

        admin_user = db.query(User).filter(User.telegram_chat_id == sender_chat_id).first()
        if not admin_user:
            admin_user = db.query(User).filter(User.role == "ADMIN").first()

        try:
            retired_doc = document_service.retire(db, biz, data["number"], by_user=admin_user)
        except document_service.DocumentError as de:
            return {"success": False, "reason": de.code, "message": f"❌ {de.message}"}

        doc_label = document_service.DOC_TYPE_LABELS.get(retired_doc.doc_type, retired_doc.doc_type)
        return {
            "success": True,
            "document_id": retired_doc.id,
            "number": data["number"],
            "message": (
                f"✅ Documento #{data['number']} (*{doc_label}*) retirado exitosamente del negocio "
                f"*{biz.commercial_name}* (NIT `{biz.nit}-{biz.dv}`)."
            ),
        }

    @classmethod
    def handle_admin_message(
        cls,
        sender_chat_id: int,
        text: str,
        db: Session,
        bot_username: str = "KontaBot",
        telegram_sender: Optional[Callable[[int, str], bool]] = None,
    ) -> str:
        """Punto de entrada unificado para despachar comandos de la administradora."""
        text_clean = text.strip()

        if text_clean.startswith("/crear_cliente"):
            res = cls.execute_crear_cliente(sender_chat_id, text_clean, db, bot_username)
            return res["message"]

        elif text_clean.startswith("/confirmar_pago"):
            res = cls.execute_confirmar_pago(sender_chat_id, text_clean, db, telegram_sender=telegram_sender)
            return res["message"]

        elif text_clean.startswith("/clientes"):
            res = cls.list_clients_summary(sender_chat_id, db)
            return res["message"]

        elif text_clean.startswith("/ejecutar_extraccion"):
            res = cls.execute_ejecutar_extraccion(sender_chat_id, text_clean, db)
            return res["message"]

        elif text_clean.startswith("/cambiar_tipo"):
            res = cls.execute_cambiar_tipo(sender_chat_id, text_clean, db)
            return res["message"]

        elif text_clean.startswith("/perfil_tributario"):
            res = cls.execute_perfil_tributario(sender_chat_id, text_clean, db)
            return res["message"]

        elif text_clean.startswith("/liberar_telegram"):
            res = cls.execute_liberar_telegram(sender_chat_id, text_clean, db)
            return res["message"]

        elif text_clean.startswith("/documentos"):
            res = cls.execute_documentos(sender_chat_id, text_clean, db)
            return res["message"]

        elif text_clean.startswith("/retirar_documento"):
            res = cls.execute_retirar_documento(sender_chat_id, text_clean, db)
            return res["message"]

        elif text_clean.startswith("/subir_documento"):
            return cls.SUBIR_DOCUMENTO_USAGE

        elif text_clean.startswith("/ayuda") or text_clean.startswith("/help"):
            return (
                "💼 *COMANDOS DE ADMINISTRACIÓN COMERCIAL (Katerinn)*\n\n"
                "1️⃣ *Crear Cliente:* Da de alta a un usuario, negocio y suscripción con enlace mágico.\n"
                "La plantilla depende del *Tipo de cliente* (primer campo: `PERSONA` o `EMPRESA`):\n\n"
                "👤 *Persona Natural:*\n"
                "`/crear_cliente PERSONA | Nombre | Celular | Cédula | Plan [| Módulo]`\n"
                "_Ejemplo (DIAN):_ `/crear_cliente PERSONA | Andrea Torres | 3001234567 | 1000000001 | TRIMESTRAL`\n"
                "_Ejemplo (Ventas):_ `/crear_cliente PERSONA | Andrea Torres | 3001234567 | 1000000001 | TRIMESTRAL | VENTAS_MANUALES`\n\n"
                "🏢 *Empresa (Representante Legal):*\n"
                "`/crear_cliente EMPRESA | Nombre Contacto | Celular | Nombre Empresa | NIT Empresa | Cédula Representante | Plan [| Módulo]`\n"
                "_Ejemplo (DIAN):_ `/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Ferretería El Roble SAS | 901008579 | 10000002 | TRIMESTRAL`\n"
                "_Ejemplo (Ventas):_ `/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Ferretería El Roble SAS | 901008579 | 10000002 | TRIMESTRAL | VENTAS_MANUALES`\n\n"
                "*Módulo* (último campo, opcional):\n"
                "• `FACTURADOR` (por defecto): factura electrónicamente, cifras e IVA desde la DIAN.\n"
                "• `VENTAS_MANUALES`: registra sus ventas a mano en el módulo de ingresos, sin IVA ni calendario tributario.\n\n"
                "2️⃣ *Confirmar Pago:* Registra transferencias, reactiva cuentas y levanta suspensiones:\n"
                "`/confirmar_pago NIT | Monto | Referencia`\n"
                "_Ejemplo:_ `/confirmar_pago 901008579 | 142500 | TR-998822`\n\n"
                "3️⃣ *Listar Clientes:* Consulta los últimos clientes registrados y su estado de vinculación:\n"
                "`/clientes`\n\n"
                "4️⃣ *Ejecutar Extracción DIAN:* Encola la descarga real de facturas de un negocio:\n"
                "`/ejecutar_extraccion NIT | Periodo`\n\n"
                "_Ejemplo (un mes):_ `/ejecutar_extraccion 901008579 | 2026-08`\n"
                "_Ejemplo (rango):_ `/ejecutar_extraccion 901008579 | 6 meses` (últimos 6 meses "
                "calendario completos, sin contar el mes en curso, en una sola solicitud)\n"
                "_Periodo opcional — si se omite, usa el mes anterior completo._\n\n"
                "5️⃣ *Cambiar tipo de negocio:* Pasa un cliente entre facturador electrónico y ventas manuales:\n"
                "`/cambiar_tipo NIT | FACTURADOR` o `/cambiar_tipo NIT | VENTAS_MANUALES`\n"
                "_Ejemplo:_ `/cambiar_tipo 901008579 | VENTAS_MANUALES`\n\n"
                "6️⃣ *Perfil tributario (solo facturadores):* Periodicidad de IVA y agente de retención, para el calendario:\n"
                "`/perfil_tributario NIT | IVA=BIMESTRAL|CUATRIMESTRAL|NINGUNO | RETENCION=SI|NO`\n"
                "_Ejemplo:_ `/perfil_tributario 901008579 | IVA=CUATRIMESTRAL | RETENCION=SI`\n\n"
                "7️⃣ *Liberar Telegram:* Desvincula un chat de Telegram asociado a una cuenta:\n"
                "`/liberar_telegram <chat_id>`\n"
                "_Ejemplo:_ `/liberar_telegram 123456789`\n\n"
                "8️⃣ *Subir Documento:* Carga un documento (PDF, PNG o JPG) al panel del cliente:\n"
                "Adjunta el archivo y escribe en el caption:\n"
                "`/subir_documento <NIT> <TIPO> [descripción]`\n"
                "_Tipos:_ `RUT`, `CAMARA`, `CEDULA`, `BANCARIA`, `OTRO`\n"
                "_Ejemplo:_ `/subir_documento 901008579 RUT`\n\n"
                "9️⃣ *Consultar Documentos:* Lista los documentos activos de un cliente:\n"
                "`/documentos <NIT>`\n"
                "_Ejemplo:_ `/documentos 901008579`\n\n"
                "🔟 *Retirar Documento:* Da de baja un documento del cliente por su número:\n"
                "`/retirar_documento <NIT> <número>`\n"
                "_Ejemplo:_ `/retirar_documento 901008579 1`"
            )

        return (
            "ℹ️ Comando no reconocido. Escribe /ayuda para ver las opciones disponibles para la administración comercial."
        )

