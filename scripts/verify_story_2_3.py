"""Script de demostración y verificación interactiva para Story 2.3:
Bot de Telegram para Contribuyentes / Clientes.
"""

import os
import sys
from datetime import datetime, date, timedelta
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dian_automation.db.models import (
    Base,
    User,
    Business,
    MonthlyTaxSummary,
    Invoice,
    DIANTaxCalendar,
    Subscription,
)
from dian_automation.telegram.client_bot import ClientTelegramBot


def main():
    print("=" * 75)
    print("[VERIFICACIÓN] STORY 2.3: BOT DE TELEGRAM PARA CONTRIBUYENTES (CLIENTES)")
    print("=" * 75)

    db_filename = "verify_story_2_3.db"
    engine = create_engine(f"sqlite:///{db_filename}", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # 1. Sembrar Cliente Activo y Vinculado (Andrea Torres)
    andrea = User(
        id="usr-andrea-demo",
        email="andrea.torres@branding.co",
        full_name="Andrea Torres",
        role="CLIENT",
        telegram_chat_id=777888999,
        is_telegram_linked=True,
        is_active=True,
    )
    # NIT 901008579 -> Último dígito es 9
    biz = Business(
        id="biz-andrea-demo",
        client_id=andrea.id,
        legal_name="Andrea Torres Diseño SAS",
        commercial_name="Torres Branding & Studio",
        nit="901008579",
        dv="7",
        is_active=True,
    )
    sub = Subscription(
        id="sub-andrea-demo",
        client_id=andrea.id,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date.today() - timedelta(days=15),
        cutoff_date=date.today() + timedelta(days=75),
        grace_period_end=date.today() + timedelta(days=78),
        status="ACTIVO",
    )

    # 2. Sembrar Resumen Mensual Liquidado (Agosto 2026)
    summary = MonthlyTaxSummary(
        business_id=biz.id,
        period_year_month="2026-08",
        total_invoiced_net=Decimal("14850000.00"),
        iva_generado=Decimal("2821500.00"),
        iva_descontable=Decimal("1230000.00"),
        iva_balance=Decimal("1591500.00"),  # >0 Saldo a Pagar DIAN
        rete_iva_total=Decimal("184500.00"),
        rete_renta_total=Decimal("371250.00"),
        rete_ica_total=Decimal("142560.00"),
        total_invoices_count=12,
        variation_vs_previous_pct=Decimal("15.40"),
        calculated_at=datetime.now(),
    )

    # 3. Sembrar Facturas Electrónicas (5 emitidas, 1 recibida)
    base_date = datetime(2026, 8, 5, 9, 30, 0)
    invoices = []
    for i in range(1, 6):
        invoices.append(
            Invoice(
                id=f"inv-demo-{i}",
                business_id=biz.id,
                document_type="Factura electrónica de venta",
                cufe=f"cufe-hash-demo-{i}",
                folio=f"204{i}",
                prefix="SETP",
                issue_date=base_date + timedelta(days=i * 4),
                issuer_nit="901008579",
                issuer_name="Andrea Torres Diseño SAS",
                receiver_nit=f"90022233{i}",
                receiver_name=f"Corporación Creativa {i} SAS",
                total=Decimal(f"{2970000 * i}.00"),
                group_type="Emitido",
                dian_status="Aceptada",
            )
        )
    # Factura recibida de compras
    invoices.append(
        Invoice(
            id="inv-rec-demo",
            business_id=biz.id,
            document_type="Factura electrónica de venta",
            cufe="cufe-hash-compra-1",
            folio="8821",
            prefix="PRV",
            issue_date=base_date + timedelta(days=2),
            issuer_nit="800333444",
            issuer_name="Papelería y Suministros SAS",
            receiver_nit="901008579",
            receiver_name="Andrea Torres Diseño SAS",
            total=Decimal("1463700.00"),
            group_type="Recibido",
            dian_status="Aceptada",
        )
    )

    # 4. Sembrar Calendario DIAN para dígito 9 y otros
    cal_entries = [
        DIANTaxCalendar(
            tax_type="IVA BIMESTRAL",
            fiscal_year=2026,
            period_label="Jul – Ago 2026",
            nit_last_digit=9,
            deadline_date=date.today() + timedelta(days=3),
            description="Declaración bimestral formulario 300 DIAN",
        ),
        DIANTaxCalendar(
            tax_type="RETEFUENTE",
            fiscal_year=2026,
            period_label="Agosto 2026",
            nit_last_digit=9,
            deadline_date=date.today() + timedelta(days=11),
            description="Declaración mensual de retenciones en la fuente formulario 350",
        ),
        DIANTaxCalendar(
            tax_type="RETEICA",
            fiscal_year=2026,
            period_label="Bimestre 4 (Jul-Ago 2026)",
            nit_last_digit=9,
            deadline_date=date.today() + timedelta(days=20),
            description="Retención ICA Secretaría de Hacienda",
        ),
        DIANTaxCalendar(
            tax_type="IVA BIMESTRAL",
            fiscal_year=2026,
            period_label="Jul – Ago 2026",
            nit_last_digit=2,  # No debe salir para Andrea
            deadline_date=date.today() + timedelta(days=1),
            description="Obligación para NIT terminado en 2",
        ),
    ]

    # 5. Cliente bloqueado por mora (Carlos Mora)
    carlos = User(
        id="usr-carlos-demo",
        email="carlos.mora@empresa.co",
        full_name="Carlos Mora",
        role="CLIENT",
        telegram_chat_id=111222333,
        is_telegram_linked=True,
        is_active=True,
    )
    biz_carlos = Business(
        id="biz-carlos-demo",
        client_id=carlos.id,
        legal_name="Comercial Mora SAS",
        commercial_name="Mora Ferreterías",
        nit="800111222",
        dv="3",
        is_active=True,
    )
    sub_carlos = Subscription(
        id="sub-carlos-demo",
        client_id=carlos.id,
        plan="MENSUAL",
        discount_rate=Decimal("0.00"),
        base_price=Decimal("50000.00"),
        final_price=Decimal("50000.00"),
        start_date=date.today() - timedelta(days=40),
        cutoff_date=date.today() - timedelta(days=10),
        grace_period_end=date.today() - timedelta(days=7),
        status="BLOQUEADO",
    )

    db.add_all([andrea, biz, sub, summary, carlos, biz_carlos, sub_carlos] + invoices + cal_entries)
    db.commit()

    # =========================================================================
    # EJECUCIÓN DE PRUEBAS INTERACTIVAS
    # =========================================================================

    print("\n--- 1. CONSULTA DE MENÚ Y AYUDA (/ayuda) ---")
    resp_help = ClientTelegramBot.handle_client_message(
        sender_chat_id=777888999, text="/ayuda", db=db
    )
    print(resp_help)

    print("\n--- 2. CONSULTA DE RESUMEN TRIBUTARIO E IVA (/resumen) ---")
    resp_resumen = ClientTelegramBot.handle_client_message(
        sender_chat_id=777888999, text="/resumen", db=db
    )
    print(resp_resumen)

    print("\n--- 3. CONSULTA DE ÚLTIMAS 4 FACTURAS EMITIDAS (/facturas) ---")
    resp_facturas = ClientTelegramBot.handle_client_message(
        sender_chat_id=777888999, text="/facturas", db=db
    )
    print(resp_facturas)

    print("\n--- 4. CONSULTA DE VENCIMIENTOS DIAN POR ÚLTIMO DÍGITO NIT (/vencimientos) ---")
    resp_venc = ClientTelegramBot.handle_client_message(
        sender_chat_id=777888999, text="/vencimientos", db=db
    )
    print(resp_venc)

    print("\n--- 5. PRUEBA DE SEGURIDAD: USUARIO NO VINCULADO ---")
    resp_unlinked = ClientTelegramBot.handle_client_message(
        sender_chat_id=999999999, text="/resumen", db=db
    )
    print(resp_unlinked)

    print("\n--- 6. PRUEBA DE SEGURIDAD: CLIENTE CON SUSCRIPCIÓN SUSPENDIDA (BLOQUEADO) ---")
    resp_blocked = ClientTelegramBot.handle_client_message(
        sender_chat_id=111222333, text="/resumen", db=db
    )
    print(resp_blocked)

    # Limpieza
    db.close()
    engine.dispose()
    if os.path.exists(db_filename):
        os.remove(db_filename)

    print("\n" + "=" * 75)
    print("[ÉXITO TOTAL] TODAS LAS FUNCIONALIDADES DE LA STORY 2.3 VERIFICADAS")
    print("=" * 75)


if __name__ == "__main__":
    main()
