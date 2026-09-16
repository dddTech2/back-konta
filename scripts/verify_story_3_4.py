"""Script de demostración y verificación interactiva para Story 3.4:
Registro de Pagos y Reactivación Automática Inmediata.
"""

import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dian_automation.db.models import Base, User, Business, Subscription, PaymentRecord
from dian_automation.telegram.admin_bot import AdminTelegramBot
from dian_automation.telegram.client_bot import ClientTelegramBot
from dian_automation.subscriptions.lockout_service import SubscriptionLockoutService


def main():
    print("=" * 80)
    print("[VERIFICACIÓN] STORY 3.4: REGISTRO DE PAGOS Y REACTIVACIÓN AUTOMÁTICA")
    print("=" * 80)

    db_filename = "verify_story_3_4.db"
    engine = create_engine(f"sqlite:///{db_filename}", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # 1. Sembrar Administradora y Cliente Suspendido por Mora
    admin = User(
        id="usr-katerinn-admin",
        email="katerinn@kontable.co",
        full_name="Katerinn Administradora",
        role="ADMIN",
        telegram_chat_id=11223344,
        is_active=True,
    )
    client = User(
        id="usr-mauricio-demo",
        email="mauricio@distribuidora.co",
        full_name="Mauricio Gómez",
        role="CLIENT",
        telegram_chat_id=888111,
        is_telegram_linked=True,
        is_active=True,
    )
    biz = Business(
        id="biz-mauricio-demo",
        client_id=client.id,
        legal_name="Distribuciones Gómez SAS",
        commercial_name="Gómez Suministros",
        nit="901008579",
        dv="7",
        is_active=True,
    )
    sub = Subscription(
        id="sub-mauricio-demo",
        client_id=client.id,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date.today() - timedelta(days=95),
        cutoff_date=date.today() - timedelta(days=5),
        grace_period_end=date.today() - timedelta(days=2),
        status="BLOQUEADO",
    )

    db.add_all([admin, client, biz, sub])
    db.commit()

    # 2. Demostración del Bloqueo Dual ANTES del Pago
    print("\n--- 1. ESTADO PREVIO AL PAGO (SERVICIO SUSPENDIDO EN WEB Y TELEGRAM) ---")
    web_pre = SubscriptionLockoutService.verify_user_web_access(client.id, db=db)
    print(f"• Acceso Web: Código {web_pre['status_code']} Forbidden (Permitido: {web_pre['allowed']})")
    print(f"  Redirección: {web_pre['redirect_url']}")

    tg_pre = ClientTelegramBot.handle_client_message(sender_chat_id=client.telegram_chat_id, text="/resumen", db=db)
    print(f"• Bot Telegram (Comando /resumen):")
    print(f"  Respuesta: {tg_pre.replace(chr(10), ' ')}")

    assert web_pre["allowed"] is False
    assert "Servicio Suspendido" in tg_pre

    # 3. Katerinn registra el pago recibido mediante comando de Telegram
    print("\n--- 2. KATERINN EJECUTA COMANDO /confirmar_pago DESDE TELEGRAM ---")
    mensajes_al_cliente = []

    def dispatch_telegram(chat_id: int, mensaje: str) -> bool:
        mensajes_al_cliente.append((chat_id, mensaje))
        return True

    comando_admin = "/confirmar_pago 901008579-7 | 142500 | BANCOLOMBIA-TR-998844"
    print(f"💼 Katerinn envía: \"{comando_admin}\"")

    resp_admin = AdminTelegramBot.execute_confirmar_pago(
        sender_chat_id=11223344,
        text=comando_admin,
        db=db,
        telegram_sender=dispatch_telegram,
    )
    assert resp_admin["success"] is True

    print("\n🤖 Bot responde a Katerinn:")
    print("------------------------------------------------------------------------")
    print(resp_admin["message"])
    print("------------------------------------------------------------------------")

    # 4. Validar persistencia de PaymentRecord y actualización de suscripción
    print("\n--- 3. VERIFICACIÓN DE TRANSACCIÓN CONTABLE Y FECHAS EN BASE DE DATOS ---")
    payment = db.query(PaymentRecord).filter(PaymentRecord.id == resp_admin["payment_id"]).first()
    db.refresh(sub)

    print(f"• Recibo de Pago ID:     {payment.id}")
    print(f"  Monto Registrado:      ${float(payment.amount):,.0f} COP")
    print(f"  Referencia Bancaria:   {payment.reference_code}")
    print(f"  Medio de Pago:         {payment.payment_method}")
    print(f"  Verificado por Admin:  {payment.verified_by_admin_id}")
    print(f"• Suscripción ID:        {sub.id}")
    print(f"  Nuevo Estado:          🟢 {sub.status}")
    print(f"  Nueva Fecha de Corte:  {sub.cutoff_date.strftime('%d/%m/%Y')}")
    print(f"  Nuevo Fin de Gracia:   {sub.grace_period_end.strftime('%d/%m/%Y')}")

    assert sub.status == "ACTIVO"
    assert payment.amount == Decimal("142500.00")

    # 5. Notificación automática recibida por el cliente
    print("\n--- 4. NOTIFICACIÓN AUTOMÁTICA DE BIENVENIDA Y REACTIVACIÓN AL CLIENTE ---")
    assert len(mensajes_al_cliente) == 1
    dest_chat, msg_cliente = mensajes_al_cliente[0]
    print(f"📲 Mensaje despachado a Chat ID {dest_chat}:")
    print("------------------------------------------------------------------------")
    print(msg_cliente)
    print("------------------------------------------------------------------------")

    # 6. Demostración del Levantamiento Inmediato del Bloqueo Dual
    print("\n--- 5. VERIFICACIÓN DE LEVANTAMIENTO INMEDIATO DEL BLOQUEO DUAL ---")
    # Canal 1: Web
    web_post = SubscriptionLockoutService.verify_user_web_access(client.id, db=db)
    print(f"• Acceso Web: Código {web_post['status_code']} OK (Permitido: {web_post['allowed']})")
    assert web_post["allowed"] is True
    print("  [OK] Plataforma web completamente desbloqueada.")

    # Canal 2: Bot de Telegram
    tg_post = ClientTelegramBot.handle_client_message(sender_chat_id=client.telegram_chat_id, text="/ayuda", db=db)
    print(f"• Bot Telegram (Comando /ayuda):")
    print(f"  Respuesta: {tg_post.split(chr(10))[0]}")
    assert "¡Hola, bienvenido a Kontable Bot!" in tg_post
    print("  [OK] Consultas tributarias en Telegram completamente reactivadas.")

    # Limpieza
    db.close()
    engine.dispose()
    if os.path.exists(db_filename):
        os.remove(db_filename)

    print("\n" + "=" * 80)
    print("[ÉXITO TOTAL] TODAS LAS FUNCIONALIDADES DE LA STORY 3.4 VERIFICADAS")
    print("=" * 80)


if __name__ == "__main__":
    main()
