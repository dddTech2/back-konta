"""Runner interactivo en tiempo real para el Bot de Telegram de Kontable.

Conecta con la API oficial de Telegram mediante Long Polling (sin requerir túneles ni ngrok).
Despacha de forma unificada:
- Vinculación atómica por Deep Linking (/start <token>)
- Comandos comerciales de Administradora (Katerinn): /crear_cliente, /confirmar_pago, /clientes
- Consultas tributarias de Clientes: /resumen, /facturas, /vencimientos, /ayuda
- Utilidades de prueba local: /mi_id, /hacerme_admin
- Notificaciones salientes en tiempo real hacia los chats de los clientes al confirmar pagos.
"""

import os
import sys
import time
import logging
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Cargar variables de entorno
load_dotenv()

from dian_automation.db.database import SessionLocal, init_db
from dian_automation.db.models import User, Business, Subscription
from dian_automation.telegram.admin_bot import AdminTelegramBot
from dian_automation.telegram.client_bot import ClientTelegramBot
from dian_automation.telegram.deep_linking import TelegramDeepLinkingService

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("telegram_runner")


class TelegramBotRunner:
    def __init__(self, bot_token: Optional[str] = None):
        self.bot_token = (
            bot_token
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or os.getenv("TELEGRAM_ADMIN_BOT_TOKEN")
            or os.getenv("TELEGRAM_CLIENT_BOT_TOKEN")
        )
        self.admin_chat_id_env = os.getenv("ADMIN_TELEGRAM_CHAT_ID")
        # Allowlist de chat_id autorizados a auto-asignarse ADMIN vía /hacerme_admin.
        # Se arma con ADMIN_BOOTSTRAP_CHAT_IDS (lista separada por comas) y/o ADMIN_TELEGRAM_CHAT_ID.
        # Si queda vacía (nada configurado), el comando sigue abierto -- conveniente para pruebas
        # locales cuando el bot no es alcanzable por nadie más -- pero basta con configurar
        # cualquiera de las dos variables para cerrar la puerta a cualquier otro chat_id.
        allowlist_raw = ",".join(
            filter(None, [os.getenv("ADMIN_BOOTSTRAP_CHAT_IDS", ""), self.admin_chat_id_env or ""])
        )
        self.admin_bootstrap_allowlist = {cid.strip() for cid in allowlist_raw.split(",") if cid.strip()}
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else ""
        self.bot_username = "KontableBot"
        self.bot_name = "Kontable"
        self.offset = 0
        self.is_running = False

    def send_message(self, chat_id: int, text: str, parse_mode: str = "Markdown") -> bool:
        """Envía un mensaje de texto a un chat específico en Telegram."""
        if not self.bot_token:
            return False
        try:
            with httpx.Client(timeout=15.0) as client:
                res = client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                    },
                )
                if res.status_code == 200:
                    return True
                else:
                    # Si falla por Markdown inválido, reintentar en texto plano
                    logger.warning(f"Error enviando mensaje con parse_mode={parse_mode}: {res.text}. Reintentando texto plano.")
                    res2 = client.post(
                        f"{self.base_url}/sendMessage",
                        json={"chat_id": chat_id, "text": text},
                    )
                    return res2.status_code == 200
        except Exception as e:
            logger.error(f"Excepción enviando mensaje a chat_id={chat_id}: {e}")
            return False

    def verify_token(self) -> bool:
        """Verifica la validez del token con getMe."""
        if not self.bot_token:
            print("\n" + "=" * 70)
            print("⚠️  TELEGRAM_BOT_TOKEN NO CONFIGURADO")
            print("=" * 70)
            print("Para conectar Telegram necesitas un token de Bot:")
            print("1. Abre Telegram y busca a @BotFather")
            print("2. Envía /newbot y sigue las instrucciones para asignarle un nombre.")
            print("3. Copia el token HTTP API generado (ej. 123456789:ABCdefGhI...).")
            print("4. Agrégalo a tu archivo .env:")
            print("   TELEGRAM_BOT_TOKEN=123456789:ABCdefGhI...\n")
            return False

        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.get(f"{self.base_url}/getMe")
                if res.status_code == 200:
                    data = res.json().get("result", {})
                    self.bot_username = data.get("username", "KontableBot")
                    self.bot_name = data.get("first_name", "Kontable")
                    return True
                else:
                    print(f"\n❌ Error validando token en Telegram API: {res.text}")
                    return False
        except Exception as e:
            print(f"\n❌ Error de red conectando con Telegram API: {e}")
            return False

    def handle_update(self, update: Dict[str, Any]):
        """Procesa una actualización individual recibida desde Telegram."""
        message = update.get("message")
        if not message:
            return

        chat = message.get("chat", {})
        chat_id = chat.get("id")
        text = message.get("text", "").strip()
        from_user = message.get("from", {})
        username = from_user.get("username")
        first_name = from_user.get("first_name", "Usuario")

        if not chat_id or not text:
            return

        logger.info(f"Mensaje de {first_name} (@{username}, chat_id={chat_id}): '{text}'")

        db = SessionLocal()
        try:
            # Sincronizar admin de .env si está configurado
            if self.admin_chat_id_env and str(chat_id) == str(self.admin_chat_id_env).strip():
                admin_user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
                if not admin_user:
                    admin_by_email = db.query(User).filter(User.email == "admin@kontable.com").first()
                    if admin_by_email:
                        admin_by_email.telegram_chat_id = chat_id
                        admin_by_email.role = "ADMIN"
                        admin_by_email.is_telegram_linked = True
                        db.commit()

            # 1. Comando de ayuda de ID / Diagnóstico
            if text in ("/mi_id", "/id", "/chat_id"):
                user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
                role_str = user.role if user else "NO REGISTRADO"
                linked_str = "SÍ" if (user and user.is_telegram_linked) else "NO"
                msg = (
                    "🆔 *INFORMACIÓN DE TU CHAT TELEGRAM*\n\n"
                    f"• *Chat ID:* `{chat_id}`\n"
                    f"• *Usuario:* @{username or 'sin_username'}\n"
                    f"• *Rol en Kontable:* `{role_str}`\n"
                    f"• *Cuenta Vinculada:* `{linked_str}`\n\n"
                    "💡 *Opciones de Prueba Rápida:*\n"
                    "- Para ser la **Administradora Comercial (Katerinn)**, escribe `/hacerme_admin`\n"
                    "- O agrega `ADMIN_TELEGRAM_CHAT_ID=" f"{chat_id}` en tu `.env`"
                )
                self.send_message(chat_id, msg)
                return

            # 2. Comando rápido para convertirse en Administradora Katerinn (ideal para pruebas locales,
            #    pero es una puerta de escalación de privilegios: si hay allowlist configurada
            #    (ADMIN_BOOTSTRAP_CHAT_IDS o ADMIN_TELEGRAM_CHAT_ID), solo esos chat_id pueden usarlo).
            if text == "/hacerme_admin":
                if self.admin_bootstrap_allowlist and str(chat_id) not in self.admin_bootstrap_allowlist:
                    self.send_message(
                        chat_id,
                        "⛔ *No autorizado*\n\n"
                        "Este chat no está habilitado para auto-asignarse el rol de administrador. "
                        "Si crees que deberías tener acceso, contacta al responsable técnico del sistema.",
                    )
                    return

                user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
                if not user:
                    # Buscar admin@kontable.com o crearlo
                    user = db.query(User).filter(User.email == "admin@kontable.com").first()
                    if not user:
                        user = User(
                            email=f"admin_{chat_id}@kontable.co",
                            full_name=f"{first_name} (Admin)",
                            role="ADMIN",
                            telegram_chat_id=chat_id,
                            telegram_username=username,
                            is_telegram_linked=True,
                            is_active=True,
                        )
                        db.add(user)
                    else:
                        user.telegram_chat_id = chat_id
                        user.telegram_username = username
                        user.role = "ADMIN"
                        user.is_telegram_linked = True
                else:
                    user.role = "ADMIN"
                    user.is_telegram_linked = True

                db.commit()
                msg = (
                    "👑 *¡Privilegios de Administradora Comercial Concedidos!*\n\n"
                    f"Hola *{user.full_name}*, ahora tienes acceso a los comandos de Katerinn:\n\n"
                    "1️⃣ *Persona:* `/crear_cliente PERSONA | Nombre | Celular | Cédula | Plan`\n"
                    "_Ejemplo:_ `/crear_cliente PERSONA | Andrea Torres | 3001234567 | 1000000001 | TRIMESTRAL`\n\n"
                    "1️⃣ *Empresa:* `/crear_cliente EMPRESA | Nombre Contacto | Celular | Nombre Empresa | NIT Empresa | Cédula Representante | Plan`\n"
                    "_Ejemplo:_ `/crear_cliente EMPRESA | Andrea Torres | 3001234567 | Ferretería El Roble SAS | 901008579 | 10000002 | TRIMESTRAL`\n\n"
                    "2️⃣ `/confirmar_pago NIT | Monto | Referencia`\n"
                    "_Ejemplo:_ `/confirmar_pago 901008579 | 142500 | TR-778899`\n\n"
                    "3️⃣ `/clientes` — Listado y estado de clientes\n"
                    "4️⃣ `/ejecutar_extraccion NIT | Periodo` — Encola una descarga real de la DIAN\n"
                    "5️⃣ `/ayuda` — Menú de administración"
                )
                self.send_message(chat_id, msg)
                return

            # 3. Flujo Deep Linking: /start <token>
            if text.startswith("/start"):
                parts = text.split(maxsplit=1)
                if len(parts) > 1 and parts[1].strip():
                    payload_token = parts[1].strip()
                    logger.info(f"Procesando Deep Link con token: {payload_token[:8]}... para chat {chat_id}")
                    link_result = TelegramDeepLinkingService.process_start_payload(
                        chat_id=chat_id,
                        payload=payload_token,
                        db=db,
                        telegram_username=username,
                    )
                    reply_text = (
                        link_result.get("welcome_message")
                        or link_result.get("message")
                        or "👋 ¡Cuenta vinculada exitosamente a Kontable!"
                    )
                    self.send_message(chat_id, reply_text)
                    return
                else:
                    # /start sin token
                    user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
                    if user and user.role == "ADMIN":
                        reply = AdminTelegramBot.handle_admin_message(
                            sender_chat_id=chat_id,
                            text="/ayuda",
                            db=db,
                            bot_username=self.bot_username,
                        )
                        self.send_message(chat_id, reply)
                        return
                    elif user and user.is_telegram_linked:
                        reply = ClientTelegramBot.handle_help(sender_chat_id=chat_id, db=db)
                        self.send_message(chat_id, reply)
                        return
                    else:
                        msg = (
                            f"👋 *¡Hola, {first_name}! Bienvenido a Kontable.*\n\n"
                            "Este es el bot de asistencia fiscal y facturación electrónica.\n\n"
                            "📱 *¿Eres cliente nuevo?*\n"
                            "Pídele a tu administradora comercial tu enlace de activación personal "
                            "o escribe `/mi_id` para ver tu identificador.\n\n"
                            "💼 *¿Eres administrador(a)?*\n"
                            "Escribe `/hacerme_admin` para activar el panel de gestión comercial."
                        )
                        self.send_message(chat_id, msg)
                        return

            # 4. Verificar si el remitente es Administrador
            user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
            is_admin = user and user.role == "ADMIN"

            if is_admin:
                # Despachador hacia AdminTelegramBot
                reply = AdminTelegramBot.handle_admin_message(
                    sender_chat_id=chat_id,
                    text=text,
                    db=db,
                    bot_username=self.bot_username,
                    telegram_sender=lambda target_cid, notif_text: self.send_message(target_cid, notif_text),
                )
                self.send_message(chat_id, reply)
                return

            # 5. Verificar si el remitente es Cliente vinculado
            if user and user.is_telegram_linked:
                # Despachador hacia ClientTelegramBot
                reply = ClientTelegramBot.handle_client_message(
                    sender_chat_id=chat_id,
                    text=text,
                    db=db,
                )
                self.send_message(chat_id, reply)
                return

            # 6. Remitente no vinculado
            msg = (
                "⛔ *Cuenta no vinculada*\n\n"
                "No encontramos ninguna cuenta de Kontable asociada a tu chat de Telegram.\n\n"
                "• Si acabas de adquirir tu plan, pulsa el enlace de invitación enviado por tu administradora.\n"
                "• Si estás realizando pruebas en local, escribe `/hacerme_admin` para usar los comandos comerciales.\n"
                "• Escribe `/mi_id` para conocer tu Chat ID."
            )
            self.send_message(chat_id, msg)

        except Exception as e:
            logger.error(f"Error procesando mensaje: {e}", exc_info=True)
            self.send_message(chat_id, f"❌ Ocurrió un error procesando tu solicitud: {e}")
        finally:
            db.close()

    def run(self):
        """Bucle principal de Long Polling."""
        init_db()

        print("\n" + "=" * 75)
        print("🤖 INICIANDO SERVIDOR BOT TELEGRAM KONTABLE (LONG POLLING)")
        print("=" * 75)

        if not self.verify_token():
            return

        print(f"🟢 Conectado con éxito como: @{self.bot_username} ({self.bot_name})")
        print("📡 Escuchando mensajes en tiempo real desde Telegram...")
        print("👉 Presiona Ctrl+C en cualquier momento para detener el bot.\n")

        self.is_running = True

        with httpx.Client(timeout=35.0) as client:
            while self.is_running:
                try:
                    url = f"{self.base_url}/getUpdates"
                    params = {
                        "offset": self.offset,
                        "timeout": 25,
                    }
                    res = client.get(url, params=params)

                    if res.status_code == 200:
                        data = res.json()
                        updates = data.get("result", [])
                        for update in updates:
                            self.offset = max(self.offset, update.get("update_id", 0) + 1)
                            self.handle_update(update)
                    elif res.status_code in (401, 404):
                        logger.error(f"Error de autenticación con Telegram: {res.text}")
                        break
                    else:
                        logger.warning(f"Respuesta inesperada de Telegram API ({res.status_code}): {res.text}")
                        time.sleep(2)

                except httpx.TimeoutException:
                    # Timeout normal de Long Polling, continuar
                    continue
                except httpx.RequestError as e:
                    logger.warning(f"Error de conexión: {e}. Reintentando en 3s...")
                    time.sleep(3)
                except KeyboardInterrupt:
                    print("\n🛑 Deteniendo el Bot de Telegram de forma segura...")
                    self.is_running = False
                    break
                except Exception as e:
                    logger.error(f"Error inesperado en polling: {e}", exc_info=True)
                    time.sleep(3)


def main():
    runner = TelegramBotRunner()
    runner.run()


if __name__ == "__main__":
    main()
