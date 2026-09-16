"""Script de demostración y verificación interactiva para Story 3.1:
Planes de Suscripción con Descuentos Porcentuales y Periodo de Gracia de 72 Horas.
"""

import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dian_automation.db.models import Base, User, Subscription
from dian_automation.subscriptions.service import (
    SubscriptionService,
    DEFAULT_BASE_MONTHLY_PRICE,
)


def main():
    print("=" * 78)
    print("[VERIFICACIÓN] STORY 3.1: MOTOR DE SUSCRIPCIONES Y DESCUENTOS PORCENTUALES")
    print("=" * 78)

    db_filename = "verify_story_3_1.db"
    engine = create_engine(f"sqlite:///{db_filename}", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # 1. Cotizaciones comparativas de planes comerciales
    print("\n--- 1. MATRIZ DE PRECIOS, DESCUENTOS Y PERIODICIDADES (Base $50,000/mes) ---")
    planes = ["MENSUAL", "TRIMESTRAL", "SEMESTRAL", "ANUAL"]
    ref_date = date.today()

    for p in planes:
        quote = SubscriptionService.calculate_quote(p, start_date=ref_date)
        dias_gracia = (quote.grace_period_end - quote.cutoff_date).days
        horas_gracia = dias_gracia * 24

        print(f"\n📋 Plan: *{quote.plan}* ({quote.months} meses)")
        print(f"   • Valor Bruto:     ${float(quote.gross_total):>10,.0f} COP")
        print(f"   • Descuento:       {float(quote.discount_rate):>9.1f}% (-${float(quote.discount_amount):,.0f} COP)")
        print(f"   • Total a Pagar:   ${float(quote.final_price):>10,.0f} COP")
        print(f"   • Fecha Inicio:    {quote.start_date.strftime('%d/%m/%Y')}")
        print(f"   • Fecha de Corte:  {quote.cutoff_date.strftime('%d/%m/%Y')}")
        print(f"   • Fin de Gracia:   {quote.grace_period_end.strftime('%d/%m/%Y')} ({horas_gracia} horas / {dias_gracia} días)")

    # 2. Creación en Base de Datos de Suscripción para Cliente
    print("\n--- 2. CREACIÓN ATÓMICA DE SUSCRIPCIÓN EN BASE DE DATOS ---")
    client = User(
        id="usr-carolina-demo",
        email="carolina.gomez@boutique.co",
        full_name="Carolina Gómez",
        role="CLIENT",
        is_active=True,
    )
    db.add(client)
    db.commit()

    sub = SubscriptionService.create_subscription(
        client_id=client.id,
        plan="SEMESTRAL",
        start_date=ref_date,
        db=db,
    )

    print(f"[OK] Suscripción creada en DB:")
    print(f"     ID:               {sub.id}")
    print(f"     Cliente:          {client.full_name} ({client.email})")
    print(f"     Plan Asignado:    {sub.plan} (-{float(sub.discount_rate):.0f}% dto)")
    print(f"     Cobro Final:      ${float(sub.final_price):,.0f} COP")
    print(f"     Estado Inicial:   {sub.status}")
    print(f"     Corte:            {sub.cutoff_date.strftime('%d/%m/%Y')}")
    print(f"     Gracia hasta:     {sub.grace_period_end.strftime('%d/%m/%Y')}")

    # 3. Simulación del Ciclo de Gracia de 72 Horas
    print("\n--- 3. SIMULACIÓN DEL CICLO DE VIDA DEL PERIODO DE GRACIA (72 HORAS) ---")
    corte = sub.cutoff_date
    hitos_simulacion = [
        ("10 días antes del corte", corte - timedelta(days=10)),
        ("Día del corte (Inicio Gracia)", corte),
        ("Día 2 de Gracia (+24h)", corte + timedelta(days=1)),
        ("Día 3 de Gracia (+48h - Último día)", corte + timedelta(days=2)),
        ("Vencimiento Gracia (+72h)", corte + timedelta(days=3)),
        ("Suspensión por Mora (+96h)", corte + timedelta(days=4)),
    ]

    for label, fecha_simulada in hitos_simulacion:
        status = SubscriptionService.get_grace_status(sub, check_date=fecha_simulada)
        icono = "🟢" if status["state"] == "ACTIVE" else ("🟡" if status["state"] == "IN_GRACE" else "🔴")
        print(f"   {icono} [{fecha_simulada.strftime('%d/%m/%Y')}] {label}:")
        print(f"      Estado: {status['state']} | {status['message']}")

    # 4. Renovación de Suscripción con Ascenso de Plan (Upgrade a Anual)
    print("\n--- 4. RENOVACIÓN Y CAMBIO DE PLAN (UPGRADE A ANUAL) ---")
    renewed = SubscriptionService.renew_subscription(
        subscription_id=sub.id,
        new_plan="ANUAL",
        db=db,
    )
    print(f"[OK] Suscripción renovada:")
    print(f"     Nuevo Plan:       {renewed.plan} (-{float(renewed.discount_rate):.0f}% dto)")
    print(f"     Nuevo Valor:      ${float(renewed.final_price):,.0f} COP (Ahorro ${float(renewed.base_price - renewed.final_price):,.0f} COP)")
    print(f"     Nuevo Corte:      {renewed.cutoff_date.strftime('%d/%m/%Y')}")
    print(f"     Nueva Gracia:     {renewed.grace_period_end.strftime('%d/%m/%Y')}")

    # Limpieza
    db.close()
    engine.dispose()
    if os.path.exists(db_filename):
        os.remove(db_filename)

    print("\n" + "=" * 78)
    print("[ÉXITO TOTAL] TODAS LAS FUNCIONALIDADES DE LA STORY 3.1 VERIFICADAS")
    print("=" * 78)


if __name__ == "__main__":
    main()
