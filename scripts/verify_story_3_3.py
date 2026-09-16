"""Script de demostración y verificación interactiva para Story 3.3:
Bloqueo Dual de Acceso en Web y Telegram al Vencer el Periodo de Gracia.
"""

import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dian_automation.db.models import Base, User, Business, Subscription, MonthlyTaxSummary
from dian_automation.subscriptions.grace_cron import SubscriptionGraceCron
from dian_automation.subscriptions.lockout_service import (
    SubscriptionLockoutService,
    SubscriptionBlockedError,
)
from dian_automation.telegram.client_bot import ClientTelegramBot


def main():
    print("=" * 80)
    print("[VERIFICACIÓN] STORY 3.3: BLOQUEO DUAL DE ACCESO EN WEB Y TELEGRAM AL VENCER GRACIA")
    print("=" * 80)

    db_filename = "verify_story_3_3.db"
    engine = create_engine(f"sqlite:///{db_filename}", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    today = date(2026, 9, 20)

    # 1. Sembrar clientes en diferentes estados
    # Cliente 1: Mauricio - Gracia vencida (Corte 15/09, Gracia venció 18/09)
    u_mauricio = User(
        id="usr-mauricio",
        email="mauricio@distribuidora.co",
        full_name="Mauricio Vencido",
        role="CLIENT",
        telegram_chat_id=5001,
        is_telegram_linked=True,
        is_active=True,
    )
    b_mauricio = Business(
        id="biz-mauricio",
        client_id=u_mauricio.id,
        legal_name="Distribuidora Vencida SAS",
        commercial_name="Vencida Distribuciones",
        nit="901222333",
        dv="5",
    )
    s_mauricio = Subscription(
        id="sub-mauricio",
        client_id=u_mauricio.id,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date(2026, 6, 15),
        cutoff_date=date(2026, 9, 15),
        grace_period_end=date(2026, 9, 18),  # Venció hace 2 días
        status="EN_MORA",
    )
    resumen_secreto = MonthlyTaxSummary(
        business_id=b_mauricio.id,
        period_year_month="2026-08",
        total_invoiced_net=Decimal("85000000.00"),
        iva_generado=Decimal("16150000.00"),
        iva_descontable=Decimal("6000000.00"),
        iva_balance=Decimal("10150000.00"),
        total_invoices_count=32,
    )

    # Cliente 2: Andrea - Al día
    u_andrea = User(
        id="usr-andrea",
        email="andrea@branding.co",
        full_name="Andrea Torres",
        role="CLIENT",
        telegram_chat_id=5002,
        is_telegram_linked=True,
        is_active=True,
    )
    b_andrea = Business(
        id="biz-andrea",
        client_id=u_andrea.id,
        legal_name="Andrea Torres Studio SAS",
        commercial_name="Torres Studio",
        nit="901008579",
        dv="7",
    )
    s_andrea = Subscription(
        id="sub-andrea",
        client_id=u_andrea.id,
        plan="SEMESTRAL",
        discount_rate=Decimal("8.00"),
        base_price=Decimal("300000.00"),
        final_price=Decimal("276000.00"),
        start_date=date(2026, 8, 1),
        cutoff_date=date(2027, 2, 1),
        grace_period_end=date(2027, 2, 4),
        status="ACTIVO",
    )

    db.add_all([u_mauricio, b_mauricio, s_mauricio, resumen_secreto, u_andrea, b_andrea, s_andrea])
    db.commit()

    # 2. Ejecutar el Cron Diario para aplicar el bloqueo
    print("\n--- 1. EJECUCIÓN DEL CRON: TRANSICIÓN A 'BLOQUEADO' AL EXPIRAR LA GRACIA ---")
    notificaciones_enviadas = []
    cron_res = SubscriptionGraceCron.run_daily_grace_check(
        db=db,
        execution_date=today,
        telegram_sender=lambda cid, msg: notificaciones_enviadas.append((cid, msg)),
    )
    print(f"[OK] Cron ejecutado para fecha {today}:")
    print(f"     Suscripciones bloqueadas por mora: {cron_res['locked_out_count']}")

    db.refresh(s_mauricio)
    assert s_mauricio.status == "BLOQUEADO"
    print(f"     Estado actualizado en DB para Mauricio: {s_mauricio.status}")

    if notificaciones_enviadas:
        print("\n📲 Notificación enviada al cliente suspendido por Telegram:")
        print("------------------------------------------------------------------------")
        print(notificaciones_enviadas[0][1])
        print("------------------------------------------------------------------------")

    # 3. Validación de Bloqueo en Canal 1: Web / API
    print("\n--- 2. VALIDACIÓN CANAL 1: BLOQUEO EN PLATAFORMA WEB / API (403 FORBIDDEN) ---")
    # Cliente Bloqueado (Mauricio)
    check_web_mauricio = SubscriptionLockoutService.verify_user_web_access(u_mauricio.id, db=db)
    print(f"• Petición Web de Mauricio (BLOQUEADO):")
    print(f"  Acceso permitido: {check_web_mauricio['allowed']}")
    print(f"  Código HTTP:      {check_web_mauricio['status_code']} Forbidden")
    print(f"  Redirección:      {check_web_mauricio['redirect_url']}")
    print(f"  Mensaje Web:      \"{check_web_mauricio['message']}\"")
    assert check_web_mauricio["allowed"] is False
    assert check_web_mauricio["status_code"] == 403
    assert check_web_mauricio["redirect_url"] == "/servicio-suspendido"

    # Verificación por Guardián de middleware (lanza excepción SubscriptionBlockedError)
    try:
        SubscriptionLockoutService.enforce_web_access(u_mauricio, db=db)
        raise AssertionError("Se esperaba SubscriptionBlockedError")
    except SubscriptionBlockedError as e:
        print(f"  [OK] Guardián lanzó SubscriptionBlockedError (Status {e.status_code}, Redirect {e.redirect_url})")

    # Cliente Activo (Andrea)
    check_web_andrea = SubscriptionLockoutService.verify_user_web_access(u_andrea.id, db=db)
    print(f"\n• Petición Web de Andrea (AL DÍA):")
    print(f"  Acceso permitido: {check_web_andrea['allowed']}")
    print(f"  Código HTTP:      {check_web_andrea['status_code']} OK")
    assert check_web_andrea["allowed"] is True

    # 4. Validación de Bloqueo en Canal 2: Bot de Telegram
    print("\n--- 3. VALIDACIÓN CANAL 2: BLOQUEO EN BOT DE TELEGRAM (SIN DATOS FISCALES) ---")
    comandos = ["/resumen", "/facturas", "/vencimientos", "/ayuda", "necesito mis facturas"]
    for cmd in comandos:
        resp_tg = ClientTelegramBot.handle_client_message(sender_chat_id=u_mauricio.telegram_chat_id, text=cmd, db=db)
        print(f"\n👤 Mauricio envía: \"{cmd}\"")
        print(f"🤖 Bot responde:")
        print(f"   {resp_tg.replace(chr(10), ' ')}")

        # Aserciones de seguridad
        assert "Tu suscripción se encuentra suspendida temporalmente por pago pendiente. Comunícate con Katerinn para reactivar tus reportes." in resp_tg
        assert "85,000,000" not in resp_tg
        assert "10,150,000" not in resp_tg
        assert "IVA Generado" not in resp_tg

    print("\n[OK] Ningún comando en Telegram revela datos tributarios ni ejecuta acciones.")

    # Limpieza
    db.close()
    engine.dispose()
    if os.path.exists(db_filename):
        os.remove(db_filename)

    print("\n" + "=" * 80)
    print("[ÉXITO TOTAL] TODAS LAS FUNCIONALIDADES DE LA STORY 3.3 VERIFICADAS")
    print("=" * 80)


if __name__ == "__main__":
    main()
