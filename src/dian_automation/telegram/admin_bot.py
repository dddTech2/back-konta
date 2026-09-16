"""Bot de Telegram para la Administradora Comercial (Katerinn).

Permite dar de alta clientes en segundos mediante plantillas (/crear_cliente),
asociar su empresa y suscripción con descuentos (Trimestral -5%, Semestral -8%, Anual -10%),
y generar automáticamente el enlace de invitación Deep Linking (/start <token>).
"""

import re
import logging
import calendar
from decimal import Decimal
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, Tuple, List, Callable
from sqlalchemy.orm import Session

from dian_automation.db.models import User, Business, Subscription, PaymentRecord
from dian_automation.subscriptions.service import add_months_to_date
from dian_automation.telegram.deep_linking import TelegramDeepLinkingService

logger = logging.getLogger("admin_bot")


class AdminTelegramBot:
    """Procesador de comandos comerciales para administradores autorizados."""

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

    @classmethod
    def calculate_dian_dv(cls, nit: str) -> str:
        """Calcula el dígito de verificación oficial de la DIAN mediante algoritmo módulo 11."""
        nit_clean = "".join(filter(str.isdigit, str(nit)))
        if not nit_clean:
            return "0"

        # Aplicar pesos desde la derecha hacia la izquierda
        nit_reversed = nit_clean[::-1]
        total = 0
        for i, digit_char in enumerate(nit_reversed):
            weight = cls.DIAN_DV_WEIGHTS[-(i + 1)] if (i + 1) <= len(cls.DIAN_DV_WEIGHTS) else 3
            total += int(digit_char) * weight

        residue = total % 11
        if residue <= 1:
            return str(residue)
        return str(11 - residue)

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
        "⚠️ *Uso incorrecto.* La plantilla depende del *Tipo de Cliente* (primer campo):\n\n"
        "👤 *Persona Natural:*\n"
        "`/crear_cliente PERSONA | Nombre Completo | Celular | Cédula | Plan`\n"
        "_Ejemplo:_ `/crear_cliente PERSONA | Andrea Torres | 3001234567 | 1000000001 | TRIMESTRAL`\n\n"
        "🏢 *Empresa (Representante Legal):*\n"
        "`/crear_cliente EMPRESA | Nombre Contacto | Celular | Nombre Empresa | NIT Empresa | Cédula Representante | Plan`\n"
        "_Ejemplo:_ `/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Ferretería El Roble SAS | 901008579 | 10000002 | TRIMESTRAL`\n\n"
        "_Planes disponibles: TRIMESTRAL, SEMESTRAL, ANUAL_"
    )

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

        return True, {
            "tipo_cliente": tipo_clean,
            "full_name": name,
            "phone": phone,
            "business_name": business_name_raw.strip() if business_name_raw else None,
            "nit": nit_clean,
            "legal_rep_doc": rep_doc_clean,
            "plan": plan_clean,
        }, None

    @classmethod
    def execute_crear_cliente(
        cls,
        sender_chat_id: int,
        text: str,
        db: Session,
        bot_username: str = "KontableBot",
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

        full_name = data["full_name"]
        phone = data["phone"]
        nit_clean = data["nit"]
        legal_rep_doc = data["legal_rep_doc"]
        tipo_cliente = data["tipo_cliente"]
        taxpayer_type = "PERSONA_JURIDICA" if tipo_cliente == "EMPRESA" else "PERSONA_NATURAL"
        # Para EMPRESA, el nombre de la razón social viene de la plantilla (nunca se inventa).
        # Para PERSONA, el propio nombre del contribuyente identifica el negocio.
        business_name = data["business_name"] if tipo_cliente == "EMPRESA" else full_name
        plan_name = data["plan"]
        plan_info = cls.PLANS_CONFIG[plan_name]

        # 3. Calcular dígito de verificación
        dv = cls.calculate_dian_dv(nit_clean)

        # 4. Verificar si ya existe un negocio o usuario cliente con este NIT o teléfono
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
                        legal_rep_doc=legal_rep_doc,
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
                    business.legal_rep_doc = legal_rep_doc
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
                    business.legal_rep_doc = legal_rep_doc
                else:
                    business = Business(
                        client_id=user.id,
                        legal_name=business_name,
                        commercial_name=business_name,
                        nit=nit_clean,
                        dv=dv,
                        taxpayer_type=taxpayer_type,
                        legal_rep_doc=legal_rep_doc,
                        is_active=True,
                    )
                    db.add(business)

            # 7. Crear o renovar Suscripción con Descuento
            today = date.today()
            cutoff = today + timedelta(days=plan_info["days"])
            grace_end = cutoff + timedelta(days=3)

            subscription = db.query(Subscription).filter(Subscription.client_id == user.id).first()
            if not subscription:
                subscription = Subscription(
                    client_id=user.id,
                    plan=plan_name,
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
                subscription.plan = plan_name
                subscription.discount_rate = plan_info["discount_rate"]
                subscription.base_price = plan_info["base_price"]
                subscription.final_price = plan_info["final_price"]
                subscription.cutoff_date = cutoff
                subscription.grace_period_end = grace_end
                subscription.status = "ACTIVO"

            db.commit()
            db.refresh(user)
            db.refresh(business)

            # 8. Generar Token y Enlace Mágico Deep Linking (72 horas)
            token_record, deep_link_url = TelegramDeepLinkingService.generate_link_token(
                user_id=user.id,
                db=db,
                expires_in_hours=72,
                bot_username=bot_username,
            )

            # 9. Construir respuesta enriquecida para Katerinn
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
                f"Admin chat {sender_chat_id} creó cliente {tipo_cliente} '{full_name}' con NIT {nit_clean}-{dv}"
                + (f" y representante {legal_rep_doc}" if legal_rep_doc else "")
            )

            return {
                "success": True,
                "user_id": user.id,
                "business_id": business.id,
                "tipo_cliente": tipo_cliente,
                "nit": f"{nit_clean}-{dv}",
                "legal_rep_doc": legal_rep_doc,
                "plan": plan_name,
                "final_price": plan_info["final_price"],
                "deep_link_url": deep_link_url,
                "message": response_message,
            }

        except Exception as e:
            db.rollback()
            logger.error(f"Error registrando cliente: {e}", exc_info=True)
            return {
                "success": False,
                "reason": "INTERNAL_ERROR",
                "message": f"❌ Error interno al crear el cliente: {str(e)}",
            }

    @classmethod
    def list_clients_summary(cls, sender_chat_id: int, db: Session, limit: int = 10) -> Dict[str, Any]:
        """Lista los clientes registrados y su estado de vinculación para la administradora."""
        if not cls.is_authorized_admin(sender_chat_id, db):
            return {
                "success": False,
                "message": "⛔ Acceso denegado.",
            }

        users = (
            db.query(User)
            .filter(User.role == "CLIENT")
            .order_by(User.created_at.desc())
            .limit(limit)
            .all()
        )

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
        biz = db.query(Business).filter(Business.nit == nit_clean).first()
        if not biz:
            return {
                "success": False,
                "reason": "BUSINESS_NOT_FOUND",
                "message": f"❌ No se encontró ningún negocio registrado con NIT `{nit_clean}`.",
            }

        client = biz.client
        if not client:
            return {
                "success": False,
                "reason": "CLIENT_NOT_FOUND",
                "message": f"❌ No existe usuario cliente asociado al negocio '{biz.commercial_name}'.",
            }

        # 4. Localizar suscripción
        sub = (
            db.query(Subscription)
            .filter(Subscription.client_id == client.id)
            .order_by(Subscription.created_at.desc())
            .first()
        )
        if not sub:
            return {
                "success": False,
                "reason": "SUBSCRIPTION_NOT_FOUND",
                "message": f"❌ El cliente {client.full_name} no tiene ninguna suscripción registrada.",
            }

        try:
            # 5. Insertar PaymentRecord
            payment = PaymentRecord(
                subscription_id=sub.id,
                amount=amount,
                payment_date=date.today(),
                payment_method="TRANSFERENCIA",
                reference_code=ref_code,
                verified_by_admin_id=admin_user.id if admin_user else None,
                notes=f"Pago confirmado vía Telegram por admin chat {sender_chat_id}",
            )
            db.add(payment)

            # 6. Reactivar y extender fechas de suscripción
            today = date.today()
            plan_cfg = cls.PLANS_CONFIG.get(sub.plan, {"months": 3})
            months_to_add = plan_cfg.get("months", 3)

            # Si el corte vigente aún está en el futuro, sumar desde el corte; sino desde hoy
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

            # 7. Enviar notificación de reactivación al cliente por Telegram
            client_reactivation_msg = (
                "🎉 *¡Pago Confirmado y Servicio Reactivado!*\n\n"
                f"Hola *{client.full_name}*, tu pago de *${amount:,.0f} COP* (Ref: `{ref_code}`) "
                "ha sido verificado y registrado exitosamente por la administración comercial.\n\n"
                f"✅ Tu suscripción para *{biz.commercial_name}* se encuentra **100% ACTIVA**.\n"
                f"📅 *Nuevo próximo corte:* {sub.cutoff_date.strftime('%d/%m/%Y')}\n"
                f"⏳ *Periodo de gracia hasta:* {sub.grace_period_end.strftime('%d/%m/%Y')}\n\n"
                "🔓 El acceso a la plataforma web y las consultas en este bot de Telegram han sido "
                "completamente restablecidos. ¡Gracias por confiar en Kontable!"
            )

            client_notified = False
            if client.is_telegram_linked and client.telegram_chat_id:
                if telegram_sender:
                    try:
                        telegram_sender(client.telegram_chat_id, client_reactivation_msg)
                        client_notified = True
                    except Exception as e:
                        logger.error(f"Error enviando mensaje de reactivación al cliente {client.telegram_chat_id}: {e}")
                else:
                    client_notified = True

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

        except Exception as e:
            db.rollback()
            logger.error(f"Error registrando pago para NIT {nit_clean}: {e}", exc_info=True)
            return {
                "success": False,
                "reason": "INTERNAL_ERROR",
                "message": f"❌ Error interno al registrar el pago: {str(e)}",
            }

    @classmethod
    def resolve_months_range(cls, n_months: int, reference_date: Optional[date] = None) -> str:
        """Calcula un único rango cubriendo los últimos N meses calendario COMPLETOS.

        El mes en curso nunca cuenta (no está completo). Si hoy es 2026-09-15 y N=6, el
        rango resultante es 2026-03-01 - 2026-08-31 (marzo a agosto, 6 meses completos).
        Se devuelve en el formato exacto que `dian_flow.resolve_target_date_range` reconoce
        como un rango explícito ('YYYY-MM-DD - YYYY-MM-DD'), para pedirlo como una sola
        exportación en vez de un job por mes.
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
        verdad contra el portal DIAN, el proceso `run_worker.py` debe estar corriendo
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

        nit_clean = data["nit"]
        period = data["period"]
        if not period:
            today = date.today()
            year, month = today.year, today.month - 1
            if month == 0:
                month, year = 12, year - 1
            period = f"{year:04d}-{month:02d}"

        biz = db.query(Business).filter(Business.nit == nit_clean).first()
        if not biz:
            return {
                "success": False,
                "reason": "BUSINESS_NOT_FOUND",
                "message": f"❌ No se encontró ningún negocio registrado con NIT `{nit_clean}`.",
            }

        from dian_automation.queue.manager import ExtractionQueueManager

        try:
            job = ExtractionQueueManager.enqueue_job(business_id=biz.id, target_period=period, db=db)
        except Exception as e:
            logger.error(f"Error encolando extracción para NIT {nit_clean}: {e}", exc_info=True)
            return {
                "success": False,
                "reason": "INTERNAL_ERROR",
                "message": f"❌ Error interno al encolar la extracción: {str(e)}",
            }

        modo = "Empresa (Representante Legal)" if biz.taxpayer_type == "PERSONA_JURIDICA" else "Persona Natural"
        periodo_label = "Rango de meses" if " - " in period else "Mes"
        message = (
            "📥 *Extracción DIAN encolada.*\n\n"
            f"🏢 *Empresa:* {biz.commercial_name} (NIT `{biz.nit}-{biz.dv}`)\n"
            f"📂 *Modo de login:* {modo}\n"
            f"📅 *{periodo_label} objetivo:* `{period}`\n"
            f"🆔 *Job:* `{job.id}`\n"
            f"⏳ *Estado:* {job.status}\n\n"
            "⚠️ *Alcance:* este comando solo agrega el trabajo a la cola. Para que se ejecute de verdad "
            "contra el portal DIAN (Chrome + Cloudflare + correo Stalwart), necesitas tener corriendo "
            "el proceso del worker, en una terminal aparte:\n"
            "`uv run python run_worker.py`\n\n"
            "El worker procesa un trabajo a la vez y avisa a Tech Ops por Telegram si algo falla."
        )

        logger.info(f"Admin chat {sender_chat_id} encoló extracción para NIT {nit_clean} periodo {period} (job {job.id})")

        return {
            "success": True,
            "job_id": job.id,
            "business_id": biz.id,
            "period": period,
            "message": message,
        }

    @classmethod
    def handle_admin_message(
        cls,
        sender_chat_id: int,
        text: str,
        db: Session,
        bot_username: str = "KontableBot",
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

        elif text_clean.startswith("/ayuda") or text_clean.startswith("/help"):
            return (
                "💼 *COMANDOS DE ADMINISTRACIÓN COMERCIAL (Katerinn)*\n\n"
                "1️⃣ *Crear Cliente:* Da de alta a un usuario, negocio y suscripción con enlace mágico.\n"
                "La plantilla depende del *Tipo* (primer campo: `PERSONA` o `EMPRESA`):\n\n"
                "👤 *Persona Natural:*\n"
                "`/crear_cliente PERSONA | Nombre | Celular | Cédula | Plan`\n"
                "_Ejemplo:_ `/crear_cliente PERSONA | Andrea Torres | 3001234567 | 1000000001 | TRIMESTRAL`\n\n"
                "🏢 *Empresa (Representante Legal):*\n"
                "`/crear_cliente EMPRESA | Nombre Contacto | Celular | Nombre Empresa | NIT Empresa | Cédula Representante | Plan`\n"
                "_Ejemplo:_ `/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Ferretería El Roble SAS | 901008579 | 10000002 | TRIMESTRAL`\n\n"
                "2️⃣ *Confirmar Pago:* Registra transferencias, reactiva cuentas y levanta suspensiones:\n"
                "`/confirmar_pago NIT | Monto | Referencia`\n"
                "_Ejemplo:_ `/confirmar_pago 901008579 | 142500 | TR-998822`\n\n"
                "3️⃣ *Listar Clientes:* Consulta los últimos clientes registrados y su estado de vinculación:\n"
                "`/clientes`\n\n"
                "4️⃣ *Ejecutar Extracción DIAN:* Encola la descarga real de facturas de un negocio:\n"
                "`/ejecutar_extraccion NIT | Periodo`\n"
                "_Ejemplo (un mes):_ `/ejecutar_extraccion 901008579 | 2026-08`\n"
                "_Ejemplo (rango):_ `/ejecutar_extraccion 901008579 | 6 meses` (últimos 6 meses "
                "calendario completos, sin contar el mes en curso, en una sola solicitud)\n"
                "_Periodo opcional — si se omite, usa el mes anterior completo._\n"
                "⚠️ _Solo encola el trabajo. Para procesarlo de verdad contra la DIAN necesitas correr por separado_ "
                "`uv run python run_worker.py`_ (un proceso aparte del bot)._"
            )

        return (
            "ℹ️ Comando no reconocido. Escribe /ayuda para ver las opciones disponibles para la administración comercial."
        )
