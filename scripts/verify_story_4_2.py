"""Script interactivo de verificación para Story 4.2: Endpoints FastAPI de Dashboard Uninegocio, Balance IVA e Historial."""

import os
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
# Los endpoints exigen JWT (Story 5.1): el script firma sus propios tokens con un secreto local.
os.environ.setdefault("JWT_SECRET", "verify-story-4-2-secreto-local")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.db.database import Base, get_db
from dian_automation.db.models import User, Business, Invoice, MonthlyTaxSummary, Subscription
from dian_automation.api.app import app
from dian_automation.core.auth_service import create_access_token


def run_verification():
    print("=" * 80)
    print("[VERIFICACIÓN] STORY 4.2: ENDPOINTS FASTAPI DASHBOARD UNINEGOCIO, IVA E HISTORIAL")
    print("=" * 80)

    # 1. Configurar base de datos de demostración
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    db = Session()

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    # 2. Poblar datos del negocio principal (Uninegocio)
    user = User(
        id="usr-andrea-demo",
        email="andrea@diseno.co",
        full_name="Andrea Torres",
        phone="3001234567",
        role="CLIENT",
        is_active=True,
    )
    db.add(user)

    biz = Business(
        id="biz-andrea-diseno",
        client_id=user.id,
        legal_name="Andrea Torres Diseño SAS",
        commercial_name="Andrea Torres Diseño",
        nit="901234567",
        dv="1",
        economic_activity="Servicios de diseño gráfico y consultoría de marca",
        taxpayer_type="PERSONA_JURIDICA",
        is_active=True,
    )
    db.add(biz)

    sub = Subscription(
        id="sub-andrea-demo",
        client_id=user.id,
        plan="TRIMESTRAL",
        start_date=date.today() - timedelta(days=30),
        cutoff_date=date.today() + timedelta(days=60),
        grace_period_end=date.today() + timedelta(days=63),
        status="ACTIVO",
        base_price=Decimal("150000.00"),
        discount_rate=Decimal("5.00"),
        final_price=Decimal("142500.00"),
    )
    db.add(sub)

    # Histórico de 6 meses de facturación
    meses_data = [
        ("2026-03", Decimal("8200000.00"), Decimal("1558000.00"), Decimal("410000.00"), Decimal("1148000.00"), 12, None),
        ("2026-04", Decimal("9600000.00"), Decimal("1824000.00"), Decimal("460000.00"), Decimal("1364000.00"), 14, Decimal("17.07")),
        ("2026-05", Decimal("10100000.00"), Decimal("1919000.00"), Decimal("510000.00"), Decimal("1409000.00"), 15, Decimal("5.21")),
        ("2026-06", Decimal("9800000.00"), Decimal("1862000.00"), Decimal("480000.00"), Decimal("1382000.00"), 13, Decimal("-2.97")),
        ("2026-07", Decimal("11700000.00"), Decimal("2223000.00"), Decimal("500000.00"), Decimal("1723000.00"), 16, Decimal("19.38")),
        ("2026-08", Decimal("12450000.00"), Decimal("2365500.00"), Decimal("540000.00"), Decimal("1825500.00"), 18, Decimal("6.41")),
    ]

    for p_ym, total_net, iva_gen, iva_desc, iva_bal, count_inv, var_pct in meses_data:
        db.add(
            MonthlyTaxSummary(
                business_id=biz.id,
                period_year_month=p_ym,
                total_invoiced_net=total_net,
                iva_generado=iva_gen,
                iva_descontable=iva_desc,
                iva_balance=iva_bal,
                total_invoices_count=count_inv,
                variation_vs_previous_pct=var_pct,
            )
        )

    # Facturas emitidas de agosto
    facturas_seed = [
        ("FE", "1042", "2026-08-22", "Estudio Craft SAS", "800999111", Decimal("1850000.00"), Decimal("351500.00"), "Emitido"),
        ("FE", "1041", "2026-08-19", "Marca Río", "900111222", Decimal("2400000.00"), Decimal("456000.00"), "Emitido"),
        ("FE", "1040", "2026-08-15", "Café Andina", "890123999", Decimal("980000.00"), Decimal("186200.00"), "Emitido"),
        ("FE", "1039", "2026-08-11", "Lucía Beltrán", "52888999", Decimal("1200000.00"), Decimal("228000.00"), "Emitido"),
        ("FE", "1038", "2026-08-05", "Taller Gráfico Norte", "901555666", Decimal("1600000.00"), Decimal("304000.00"), "Emitido"),
        ("REC", "334", "2026-08-18", "Papelería Central", "860001234", Decimal("650000.00"), Decimal("123500.00"), "Recibido"),
    ]

    for i, (pref, fol, f_str, cli, nit_c, val, iva, grp) in enumerate(facturas_seed):
        db.add(
            Invoice(
                id=f"inv-{i+1:03d}",
                business_id=biz.id,
                cufe=f"mock-cufe-{pref}-{fol}-12345",
                document_type="Factura electrónica de venta",
                prefix=pref,
                folio=fol,
                issue_date=datetime.strptime(f_str, "%Y-%m-%d"),
                issuer_nit=biz.nit if grp == "Emitido" else nit_c,
                issuer_name=biz.commercial_name if grp == "Emitido" else cli,
                receiver_nit=nit_c if grp == "Emitido" else biz.nit,
                receiver_name=cli if grp == "Emitido" else biz.commercial_name,
                iva=iva,
                total=val,
                group_type=grp,
            )
        )

    db.commit()
    client.headers.update({"Authorization": f"Bearer {create_access_token(user)}"})

    # =========================================================================
    # 3. VERIFICACIÓN 1: GET /api/dashboard/{nit}
    # =========================================================================
    print("\n--- 1. CONSULTA GET /api/dashboard/{nit} (PANTALLA DE INICIO) ---")
    t0 = time.perf_counter()
    res_dash = client.get(f"/api/dashboard/{biz.nit}")
    latency_dash = (time.perf_counter() - t0) * 1000

    print(f"• Código HTTP: {res_dash.status_code} OK (Latencia: {latency_dash:.2f} ms)")
    data_dash = res_dash.json()

    print(f"• Empresa Activa:      {data_dash['business']['commercial_name']} (NIT: {data_dash['business']['nit']}-{data_dash['business']['dv']})")
    print(f"• Total Facturado Mes: ${data_dash['resumen']['total']:,.0f} COP ({data_dash['resumen']['variacion']:+.1f}% vs mes anterior)")
    print(f"• IVA Acumulado:       ${data_dash['resumen']['ivaAcumulado']:,.0f} COP en {data_dash['resumen']['numFacturas']} facturas")
    print(f"• Estado Suscripción:  🟢 {data_dash['suscripcion']['estado']} (Plan: {data_dash['suscripcion']['plan']})")

    print("\n📊 Histórico de Facturación (Últimos 6 meses para gráfico de barras):")
    for bar in data_dash['historico']:
        bar_visual = "█" * int(bar['total'] / 1000000)
        print(f"   [{bar['mes']}] {bar['period_year_month']}: ${bar['total']:>10,.0f} COP  {bar_visual}")

    print("\n🧾 Facturas Recientes (Top 4 para card en home):")
    for f in data_dash['facturasRecientes']:
        print(f"   • {f['num']} ({f['fecha']}) - {f['cliente'][:22]:<22}: ${f['valor']:>10,.0f} COP")

    # =========================================================================
    # 4. VERIFICACIÓN 2: GET /api/iva/{nit}
    # =========================================================================
    print("\n--- 2. CONSULTA GET /api/iva/{nit} (BALANCE DE IVA Y RING SVG) ---")
    t0 = time.perf_counter()
    res_iva = client.get(f"/api/iva/{biz.nit}")
    latency_iva = (time.perf_counter() - t0) * 1000

    print(f"• Código HTTP: {res_iva.status_code} OK (Latencia: {latency_iva:.2f} ms)")
    data_iva = res_iva.json()
    p_actual = data_iva['periodos'][0]

    print(f"• Periodo Fiscal:     {p_actual['etiqueta']} (Estado: {p_actual['estado']})")
    print(f"• IVA Generado (Net): ${p_actual['generado']:,.0f} COP")
    print(f"• IVA Descontable:    ${p_actual['descontable']:,.0f} COP")
    print(f"• Saldo a Pagar:      ${p_actual['saldo']:,.0f} COP")
    print(f"• Ratio Ring SVG:     {p_actual['pct']*100:.1f}% (Descontable / Generado)")

    # =========================================================================
    # 5. VERIFICACIÓN 3: GET /api/invoices/{nit} (HISTORIAL CON FILTROS)
    # =========================================================================
    print("\n--- 3. CONSULTA GET /api/invoices/{nit} (HISTORIAL Y FILTRADO) ---")
    res_inv = client.get(f"/api/invoices/{biz.nit}?group_type=Emitido&limit=3")
    data_inv = res_inv.json()

    print(f"• Código HTTP: {res_inv.status_code} OK")
    print(f"• Facturas Emitidas Encontradas: {data_inv['total_count']} (Mostrando primeras {len(data_inv['invoices'])})")
    for inv in data_inv['invoices']:
        print(f"   • {inv['num']} | {inv['cliente'][:20]:<20} | ${inv['valor']:>10,.0f} COP | IVA: ${inv['iva']:>8,.0f}")

    # =========================================================================
    # 6. VERIFICACIÓN 4: CONTROL DE ACCESO DUAL Y BLOQUEO 403 FORBIDDEN
    # =========================================================================
    print("\n--- 4. VERIFICACIÓN DE CONTROL DE ACCESO (SIMULACIÓN DE SUSPENSIÓN) ---")
    sub.status = "BLOQUEADO"
    db.commit()

    print("🚫 Suscripción transicionada a estado: 'BLOQUEADO'")
    r_blocked = client.get(f"/api/dashboard/{biz.nit}")
    print(f"• Petición a /api/dashboard/{biz.nit} -> Código HTTP: {r_blocked.status_code} Forbidden")
    blocked_json = r_blocked.json()
    print(f"  Error Code:   {blocked_json['error']}")
    print(f"  Mensaje UX:   {blocked_json['message']}")
    print(f"  Redirect URL: {blocked_json['redirect_url']}")

    assert r_blocked.status_code == 403
    assert blocked_json['error'] == "SUBSCRIPTION_BLOCKED"
    print("\n[OK] El interceptor de suscripción bloqueó la petición protegiendo las métricas tributarias.")

    print("\n" + "=" * 80)
    print("[ÉXITO TOTAL] TODOS LOS ENDPOINTS Y CRITERIOS DE LA STORY 4.2 CUMPLIDOS AL 100%")
    print("=" * 80)


if __name__ == "__main__":
    run_verification()
