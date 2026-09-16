"""Rutas para el Dashboard principal de Kontable."""

from typing import Tuple, Dict, Any, List
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from dian_automation.db.database import get_db
from dian_automation.db.models import Business, User, MonthlyTaxSummary, Invoice, Subscription
from dian_automation.api.dependencies import get_business_with_access
from dian_automation.api.schemas import (
    DashboardResponse,
    BusinessInfo,
    MetricSummary,
    MonthlyBar,
    RecentInvoice,
    NextTaxAlert,
    SubscriptionInfo,
)

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])

MONTH_NAMES = {
    "01": "Ene", "02": "Feb", "03": "Mar", "04": "Abr",
    "05": "May", "06": "Jun", "07": "Jul", "08": "Ago",
    "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dic",
}


def _format_month_name(period_ym: str) -> str:
    """Convierte '2026-08' en 'Ago'."""
    parts = period_ym.split("-")
    if len(parts) == 2:
        return MONTH_NAMES.get(parts[1], parts[1])
    return period_ym


def _format_short_date(dt) -> str:
    """Convierte datetime en formato amigable '22 ago'."""
    if not dt:
        return ""
    m = MONTH_NAMES.get(f"{dt.month:02d}", "").lower()
    return f"{dt.day} {m}"


@router.get("/{business_id}", response_model=DashboardResponse)
def get_dashboard(
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    db: Session = Depends(get_db),
):
    """Retorna todas las métricas consolidadas consumidas por la pantalla de inicio."""
    business, user, access_check = business_access

    # 1. Información del negocio
    biz_info = BusinessInfo(
        id=business.id,
        commercial_name=business.commercial_name,
        legal_name=business.legal_name,
        nit=business.nit,
        dv=business.dv,
        economic_activity=business.economic_activity or "Servicios / Comercio",
        taxpayer_type=business.taxpayer_type,
    )

    # 2. Resumen del mes activo
    summaries = (
        db.query(MonthlyTaxSummary)
        .filter(MonthlyTaxSummary.business_id == business.id)
        .order_by(MonthlyTaxSummary.period_year_month.desc())
        .all()
    )

    if summaries:
        latest = summaries[0]
        resumen = MetricSummary(
            total=float(latest.total_invoiced_net),
            ivaAcumulado=float(latest.iva_generado),
            numFacturas=latest.total_invoices_count,
            variacion=float(latest.variation_vs_previous_pct or 0.0),
            periodo=latest.period_year_month,
        )
    else:
        # Si aún no hay resúmenes consolidados
        resumen = MetricSummary(
            total=0.0,
            ivaAcumulado=0.0,
            numFacturas=0,
            variacion=0.0,
            periodo="2026-08",
        )

    # 3. Histórico de últimos 6 meses (para gráfico de barras)
    recent_summaries = summaries[:6]
    recent_summaries.reverse()  # Orden cronológico asc
    historico: List[MonthlyBar] = [
        MonthlyBar(
            mes=_format_month_name(s.period_year_month),
            period_year_month=s.period_year_month,
            total=float(s.total_invoiced_net),
        )
        for s in recent_summaries
    ]

    # Si no hay histórico, creamos una barra con el periodo actual
    if not historico:
        historico = [
            MonthlyBar(mes="Ago", period_year_month="2026-08", total=resumen.total)
        ]

    # 4. Facturas recientes emitidas (máximo 4 para el hero/card)
    recent_invoices_db = (
        db.query(Invoice)
        .filter(Invoice.business_id == business.id, Invoice.group_type == "Emitido")
        .order_by(Invoice.issue_date.desc())
        .limit(4)
        .all()
    )

    facturas_recientes: List[RecentInvoice] = []
    for inv in recent_invoices_db:
        num_str = f"{inv.prefix or 'FE'}-{inv.folio}" if inv.folio else (inv.cufe[:8] if inv.cufe else "FE")
        facturas_recientes.append(
            RecentInvoice(
                id=inv.id,
                num=num_str,
                fecha=_format_short_date(inv.issue_date),
                cliente=inv.receiver_name or "Consumidor final",
                valor=float(inv.total),
                iva=float(inv.iva),
                tipo="Emitido",
            )
        )

    # 5. Alerta de próximo vencimiento tributario
    alerta = NextTaxAlert(
        dias=5,
        etiqueta=f"Periodo {resumen.periodo}",
        limite="10 prox. mes",
        estado="proximo",
    )

    # 6. Información de suscripción
    sub = (
        db.query(Subscription)
        .filter(Subscription.client_id == user.id)
        .order_by(Subscription.created_at.desc())
        .first()
    )

    sub_info = SubscriptionInfo(
        estado=sub.status if sub else "ACTIVO",
        plan=sub.plan if sub else "TRIMESTRAL",
        has_warning_banner=access_check.get("has_warning_banner", False),
        days_left_in_grace=access_check.get("days_left_in_grace"),
        cutoff_date=sub.cutoff_date.isoformat() if sub and sub.cutoff_date else None,
        grace_period_end=sub.grace_period_end.isoformat() if sub and sub.grace_period_end else None,
    )

    return DashboardResponse(
        business=biz_info,
        resumen=resumen,
        historico=historico,
        facturasRecientes=facturas_recientes,
        alertaProximoVencimiento=alerta,
        suscripcion=sub_info,
    )
