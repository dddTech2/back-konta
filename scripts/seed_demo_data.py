"""Script para sembrar datos de demostración en kontable.db.

Garantiza que la base de datos local tenga:
1. Usuario Administrador (Katerinn)
2. Negocio Activo (Vet Demo NIT 901008579-7)
3. Resumen fiscal mensual de IVA y Facturación (2026-08)
4. Entradas en el Calendario Tributario DIAN para el dígito 9
"""

import sys
import os
from decimal import Decimal
from datetime import datetime, date, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dian_automation.db.database import SessionLocal, init_db, engine
from dian_automation.db.models import Base, User, Business, Subscription, MonthlyTaxSummary, DIANTaxCalendar


def seed():
    print("🌱 Inicializando base de datos...")
    init_db()
    db = SessionLocal()

    try:
        # 1. Asegurar Admin Katerinn
        admin = db.query(User).filter(User.email == "admin@kontable.com").first()
        if not admin:
            admin = User(
                id="usr-katerinn-admin",
                email="admin@kontable.com",
                full_name="Katerinn Administradora",
                phone="+573001234567",
                role="ADMIN",
                is_telegram_linked=False,
                is_active=True,
            )
            db.add(admin)
            print("   [+] Creado usuario ADMIN: admin@kontable.com")
        else:
            admin.role = "ADMIN"
            admin.full_name = "Katerinn Administradora"
            print("   [OK] Usuario ADMIN verificado: admin@kontable.com")

        # 2. Asegurar Negocio Vet Demo
        biz = db.query(Business).filter(Business.nit == "901008579").first()
        if not biz:
            biz = Business(
                id="biz-vetdemo-demo",
                nit="901008579",
                dv="7",
                legal_name="VETERINARIA DEMO S.A.S.",
                commercial_name="Vet Demo",
                user_id=admin.id,
                is_active=True,
            )
            db.add(biz)
            print("   [+] Creado negocio: Vet Demo (901008579-7)")
        else:
            print(f"   [OK] Negocio verificado: {biz.commercial_name} (NIT {biz.nit}-{biz.dv})")

        # 3. Asegurar Suscripción Activa para Vet Demo (client_id = biz.client_id)
        sub = db.query(Subscription).filter(Subscription.client_id == biz.client_id).first()
        if not sub:
            today = date.today()
            cutoff = today + timedelta(days=25)
            grace = cutoff + timedelta(days=3)
            sub = Subscription(
                id="sub-vetdemo-trimestral",
                client_id=biz.client_id,
                plan="TRIMESTRAL",
                status="ACTIVO",
                base_price=Decimal("150000.00"),
                discount_rate=Decimal("5.00"),
                final_price=Decimal("142500.00"),
                start_date=today - timedelta(days=65),
                cutoff_date=cutoff,
                grace_period_end=grace,
            )
            db.add(sub)
            print(f"   [+] Creada suscripción TRIMESTRAL activa hasta {cutoff}")
        else:
            print(f"   [OK] Suscripción verificada: Estado={sub.status}, Corte={sub.cutoff_date}")

        # 4. Asegurar Resumen Mensual (si no existiera)
        summary = db.query(MonthlyTaxSummary).filter(
            MonthlyTaxSummary.business_id == biz.id,
            MonthlyTaxSummary.period_year_month == "2026-08"
        ).first()
        if not summary:
            summary = MonthlyTaxSummary(
                id="mts-vetdemo-2026-08",
                business_id=biz.id,
                period_year_month="2026-08",
                total_invoiced_net=Decimal("45341427.00"),
                iva_generado=Decimal("2968824.18"),
                iva_descontable=Decimal("684806.61"),
                iva_balance=Decimal("2284017.57"),
                total_invoices_count=701,
            )
            db.add(summary)
            print("   [+] Creado resumen fiscal 2026-08")
        else:
            print("   [OK] Resumen fiscal 2026-08 verificado")

        # 5. Sembrar Calendario Tributario para Dígito 9 (NIT 901008579)
        existing_cal = db.query(DIANTaxCalendar).filter(DIANTaxCalendar.nit_last_digit == 9).count()
        if existing_cal == 0:
            today = date.today()
            obligations = [
                (
                    "Retención en la Fuente",
                    "Periodo Mensual Sep",
                    today + timedelta(days=4),
                    "Declaración y pago mensual de retenciones en la fuente (Formulario 350).",
                ),
                (
                    "Impuesto sobre las Ventas (IVA)",
                    "Bimestre 5 (Sep-Oct)",
                    today + timedelta(days=18),
                    "Declaración bimestral de IVA régimen común (Formulario 300).",
                ),
                (
                    "Impuesto de Renta PJ",
                    "Anticipo Cuota 2",
                    today + timedelta(days=45),
                    "Segunda cuota del impuesto sobre la renta personas jurídicas.",
                ),
                (
                    "Retención ICA Municipal",
                    "Bimestre 5",
                    today + timedelta(days=12),
                    "Declaración y pago bimestral de ReteICA en Bogotá.",
                ),
            ]
            for tax_type, period_label, deadline, desc in obligations:
                entry = DIANTaxCalendar(
                    tax_type=tax_type,
                    fiscal_year=today.year,
                    period_label=period_label,
                    nit_last_digit=9,
                    deadline_date=deadline,
                    description=desc,
                )
                db.add(entry)
            print(f"   [+] Sembradas {len(obligations)} obligaciones en Calendario Tributario para NIT terminado en 9")
        else:
            print(f"   [OK] Calendario Tributario ya cuenta con {existing_cal} obligaciones para dígito 9")

        db.commit()
        print("\n✨ ¡Datos de demostración sembrados exitosamente en kontable.db!")

    except Exception as e:
        db.rollback()
        print(f"\n❌ Error sembrando datos: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
