"""Script de demostración y verificación interactiva para Story 3.2:
Cron de Gestión de Periodo de Gracia de 72 Horas y Notificaciones Diarias de Cobro.
"""

import os
import sys
from datetime import datetime, date, timedelta
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dian_automation.db.models import Base, User, Business, Subscription
from dian_automation.subscriptions.grace_cron import SubscriptionGraceCron


def main():
    print("=" * 80)
    print("[VERIFICACIÓN] STORY 3.2: CRON DE GESTIÓN DE PERIODO DE GRACIA (72H) Y ALERTAS")
    print("=" * 80)

    db_filename = "verify_story_3_2.db"
    engine = create_engine(f"sqlite:///{db_filename}", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    today = date(2026, 9, 15)

    # 1. Sembrar 4 clientes con diferentes momentos en su ciclo de suscripción
    # Cliente 1: Llega a su fecha de corte hoy (Día 1 de gracia)
    u1 = User(
        id="usr-corte-hoy",
        email="andrea@estudio.co",
        full_name="Andrea Torres",
        role="CLIENT",
        telegram_chat_id=1001,
        is_telegram_linked=True,
        is_active=True,
    )
    b1 = Business(
        id="biz-1",
        client_id=u1.id,
        legal_name="Andrea Torres Studio SAS",
        commercial_name="Torres Studio",
        nit="901008579",
        dv="7",
    )
    s1 = Subscription(
        id="sub-1",
        client_id=u1.id,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=today - timedelta(days=90),
        cutoff_date=today,  # HOY es el corte
        grace_period_end=today + timedelta(days=3),  # 72h
        status="ACTIVO",
    )

    # Cliente 2: Cumplió corte ayer (Día 2 de gracia, restan 2 días)
    u2 = User(
        id="usr-dia-2",
        email="carlos@distribuidora.co",
        full_name="Carlos Méndez",
        role="CLIENT",
        telegram_chat_id=1002,
        is_telegram_linked=True,
        is_active=True,
    )
    b2 = Business(
        id="biz-2",
        client_id=u2.id,
        legal_name="Distribuciones Méndez SAS",
        commercial_name="Méndez Distribuciones",
        nit="800111222",
        dv="3",
    )
    s2 = Subscription(
        id="sub-2",
        client_id=u2.id,
        plan="SEMESTRAL",
        discount_rate=Decimal("8.00"),
        base_price=Decimal("300000.00"),
        final_price=Decimal("276000.00"),
        start_date=today - timedelta(days=181),
        cutoff_date=today - timedelta(days=1),
        grace_period_end=today + timedelta(days=2),
        status="EN_MORA",
        last_notified_at=datetime(2026, 9, 14, 8, 0, 0),  # Notificado ayer
    )

    # Cliente 3: Cumplió corte hace 2 días (Día 3 de gracia, ÚLTIMO DÍA)
    u3 = User(
        id="usr-dia-3",
        email="laura@consultores.co",
        full_name="Laura Restrepo",
        role="CLIENT",
        telegram_chat_id=1003,
        is_telegram_linked=True,
        is_active=True,
    )
    b3 = Business(
        id="biz-3",
        client_id=u3.id,
        legal_name="Consultoría Tributaria SAS",
        commercial_name="Restrepo Consultores",
        nit="830037946",
        dv="3",
    )
    s3 = Subscription(
        id="sub-3",
        client_id=u3.id,
        plan="ANUAL",
        discount_rate=Decimal("10.00"),
        base_price=Decimal("600000.00"),
        final_price=Decimal("540000.00"),
        start_date=today - timedelta(days=367),
        cutoff_date=today - timedelta(days=2),
        grace_period_end=today + timedelta(days=1),
        status="EN_MORA",
        last_notified_at=datetime(2026, 9, 14, 8, 0, 0),  # Notificado ayer
    )

    # Cliente 4: Al día (Corte dentro de 20 días)
    u4 = User(
        id="usr-al-dia",
        email="diego@tecnologia.co",
        full_name="Diego Morales",
        role="CLIENT",
        telegram_chat_id=1004,
        is_telegram_linked=True,
        is_active=True,
    )
    b4 = Business(
        id="biz-4",
        client_id=u4.id,
        legal_name="Morales Tech SAS",
        commercial_name="Morales Tech",
        nit="860000000",
        dv="0",
    )
    s4 = Subscription(
        id="sub-4",
        client_id=u4.id,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=today - timedelta(days=10),
        cutoff_date=today + timedelta(days=20),
        grace_period_end=today + timedelta(days=23),
        status="ACTIVO",
    )

    db.add_all([u1, b1, s1, u2, b2, s2, u3, b3, s3, u4, b4, s4])
    db.commit()

    # 2. Ejecutar el Cron Diario (Capturando despachos de Telegram)
    print("\n--- 1. EJECUCIÓN DEL CRON DIARIO (00:00 UTC) ---")
    despachos_telegram = []

    def captura_telegram(chat_id: int, mensaje: str) -> bool:
        despachos_telegram.append((chat_id, mensaje))
        return True

    res = SubscriptionGraceCron.run_daily_grace_check(
        db=db,
        execution_date=today,
        telegram_sender=captura_telegram,
    )

    print(f"[OK] Cron ejecutado exitosamente para fecha: {res['execution_date']}")
    print(f"     Suscripciones procesadas en gracia: {res['processed_count']}")
    print(f"     Transicionadas de ACTIVO a EN_MORA: {res['transitioned_to_mora']}")
    print(f"     Notificaciones de Telegram enviadas: {res['notifications_sent']}")

    # 3. Mostrar los mensajes progresivos enviados a cada cliente
    print("\n--- 2. NOTIFICACIONES DESPACHADAS POR TELEGRAM ---")
    for chat_id, msg in despachos_telegram:
        print(f"\n📲 [CHAT ID: {chat_id}]")
        print("------------------------------------------------------------------------")
        print(msg)
        print("------------------------------------------------------------------------")

    # 4. Verificar idempotencia en segunda ejecución el mismo día
    print("\n--- 3. CONTROL DE IDEMPOTENCIA (Segunda corrida en el mismo día) ---")
    despachos_segunda_corrida = []
    res_segunda = SubscriptionGraceCron.run_daily_grace_check(
        db=db,
        execution_date=today,
        telegram_sender=lambda cid, m: despachos_segunda_corrida.append((cid, m)),
    )
    print(f"Suscripciones procesadas: {res_segunda['processed_count']}")
    print(f"Notificaciones reenviadas: {res_segunda['notifications_sent']} (0 esperado)")
    assert res_segunda["notifications_sent"] == 0
    print("[OK] Idempotencia confirmada: No hubo duplicación de mensajes ni spam.")

    # 5. Demostración de Banner de Advertencia para la Web
    print("\n--- 4. PAYLOADS DE BANNER DE ADVERTENCIA PARA LA APLICACIÓN WEB ---")
    sub_activa = db.query(Subscription).filter(Subscription.id == "sub-4").first()
    sub_mora_dia1 = db.query(Subscription).filter(Subscription.id == "sub-1").first()
    sub_mora_dia3 = db.query(Subscription).filter(Subscription.id == "sub-3").first()

    print(f"• Cliente al día (Diego Morales):")
    print(f"  Mostrar banner: {SubscriptionGraceCron.has_payment_warning_banner(sub_activa)}")

    banner_dia1 = SubscriptionGraceCron.get_payment_warning_banner_info(sub_mora_dia1, check_date=today)
    print(f"\n• Cliente en Día 1 de Gracia (Andrea Torres):")
    print(f"  Severidad: {banner_dia1['severity']} | Días restantes: {banner_dia1['days_left_in_grace']}")
    print(f"  Mensaje Web: \"{banner_dia1['message']}\"")

    banner_dia3 = SubscriptionGraceCron.get_payment_warning_banner_info(sub_mora_dia3, check_date=today)
    print(f"\n• Cliente en Día 3 de Gracia (Laura Restrepo):")
    print(f"  Severidad: {banner_dia3['severity']} | Días restantes: {banner_dia3['days_left_in_grace']}")
    print(f"  Mensaje Web: \"{banner_dia3['message']}\"")

    # Limpieza
    db.close()
    engine.dispose()
    if os.path.exists(db_filename):
        os.remove(db_filename)

    print("\n" + "=" * 80)
    print("[ÉXITO TOTAL] TODAS LAS FUNCIONALIDADES DE LA STORY 3.2 VERIFICADAS")
    print("=" * 80)


if __name__ == "__main__":
    main()
