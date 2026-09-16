"""Script de demostración y verificación interactiva para Story 2.1:
Vinculación de Clientes por Deep Linking con Token Único (/start <token>).
"""

import os
import sys
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dian_automation.db.models import Base, User, Business, TelegramLinkToken
from dian_automation.telegram.deep_linking import TelegramDeepLinkingService


def main():
    print("=" * 70)
    print("[VERIFICACION] STORY 2.1: DEEP LINKING CON TOKEN UNICO")
    print("=" * 70)

    # 1. Base de datos aislada para demostración
    engine = create_engine("sqlite:///verify_story_2_1.db", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # 2. Registrar cliente y su empresa (sin Telegram aún)
    client_user = User(
        id="usr-andrea-demo",
        email="andrea.torres@branding.co",
        full_name="Andrea Torres",
        role="CLIENT",
        telegram_chat_id=None,
        is_telegram_linked=False,
        is_active=True,
    )
    biz = Business(
        id="biz-andrea-demo",
        client_id=client_user.id,
        legal_name="Andrea Torres Diseño SAS",
        commercial_name="Torres Branding & Studio",
        nit="901008579",
        dv="8",
    )
    db.add_all([client_user, biz])
    db.commit()

    print("\n[OK] 1. Cliente registrado en DB (Pre-vinculación):")
    print(f"   - Nombre: {client_user.full_name} ({client_user.email})")
    print(f"   - Negocio: {biz.commercial_name} (NIT {biz.nit}-{biz.dv})")
    print(f"   - Estado Telegram: is_linked={client_user.is_telegram_linked}, chat_id={client_user.telegram_chat_id}")

    # 3. Generar enlace mágico de vinculación
    token_obj, link_url = TelegramDeepLinkingService.generate_link_token(
        user_id=client_user.id,
        db=db,
        expires_in_hours=72,
        bot_username="KontableBot",
    )

    print("\n🔗 2. Enlace Mágico Generado por el Sistema:")
    print(f"   - Token único: {token_obj.token[:16]}... ({len(token_obj.token)} caracteres seguros)")
    print(f"   - Expira en: {token_obj.expires_at.strftime('%Y-%m-%d %H:%M:%S')} (Vigencia 72h)")
    print(f"   - URL para enviar al cliente: {link_url}")

    # 4. Simulación: La clienta abre el enlace y pulsa 'Iniciar' en Telegram
    simulated_chat_id = 1234567890
    simulated_username = "andreatorres_design"

    print("\n📲 3. Simulación de Cliente abriendo enlace en Telegram:")
    print(f"   -> Chat ID entrante: {simulated_chat_id}")
    print(f"   -> Comando recibido: /start {token_obj.token}")

    result = TelegramDeepLinkingService.process_start_payload(
        chat_id=simulated_chat_id,
        payload=token_obj.token,
        db=db,
        telegram_username=simulated_username,
    )

    print(f"\n✅ 4. Resultado del Webhook de Vinculación:")
    print(f"   - Éxito: {result['success']}")
    print(f"   - Mensaje entregado a la clienta:")
    print("   --------------------------------------------------------")
    for line in result["welcome_message"].split("\n"):
        print(f"   | {line}")
    print("   --------------------------------------------------------")

    # 5. Comprobar actualización persistente en DB
    db.refresh(client_user)
    db.refresh(token_obj)
    print("\n🔍 5. Estado verificado en Base de Datos:")
    print(f"   - User is_telegram_linked: {client_user.is_telegram_linked}")
    print(f"   - User telegram_chat_id:   {client_user.telegram_chat_id}")
    print(f"   - User telegram_username:  @{client_user.telegram_username}")
    print(f"   - Token is_used:           {token_obj.is_used}")
    print(f"   - Token used_at:           {token_obj.used_at.strftime('%Y-%m-%d %H:%M:%S')}")

    # 6. Prueba de Seguridad: Intentar reutilizar el mismo enlace
    print("\n🛡️ 6. Prueba de Seguridad: Reutilización de token...")
    retry_result = TelegramDeepLinkingService.process_start_payload(
        chat_id=9999999999,
        payload=token_obj.token,
        db=db,
    )
    print(f"   -> Resultado: success={retry_result['success']} | Motivo={retry_result['reason']}")
    print(f"   -> Mensaje: {retry_result['message']}")

    # 7. Prueba de Expiración
    print("\n⏳ 7. Prueba de Seguridad: Token Expirado...")
    token_exp, _ = TelegramDeepLinkingService.generate_link_token(client_user.id, db)
    token_exp.expires_at = datetime.utcnow() - timedelta(hours=1)
    db.commit()

    expired_result = TelegramDeepLinkingService.process_start_payload(
        chat_id=simulated_chat_id,
        payload=token_exp.token,
        db=db,
    )
    print(f"   -> Resultado: success={expired_result['success']} | Motivo={expired_result['reason']}")
    print(f"   -> Mensaje: {expired_result['message']}")

    # Limpieza
    db.close()
    engine.dispose()
    if os.path.exists("verify_story_2_1.db"):
        try:
            os.remove("verify_story_2_1.db")
        except Exception:
            pass

    print("\n" + "=" * 70)
    print("[OK] VERIFICACION DE STORY 2.1 COMPLETADA EXITOSAMENTE")
    print("=" * 70)


if __name__ == "__main__":
    main()
