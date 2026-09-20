"""Rutas para la consulta de Balance de IVA y Ring SVG."""

from typing import Tuple, Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from dian_automation.db.database import get_db
from dian_automation.db.models import Business, User, MonthlyTaxSummary, Invoice, INCOME_SOURCE_MANUAL_SALES
from dian_automation.api.dependencies import get_business_with_access
from dian_automation.api.schemas import (
    IvaDetailResponse,
    IvaPeriodItem,
    PeriodInvoiceItem,
)
from dian_automation.api.routes_dashboard import _format_short_date
from dian_automation.core.calendar_engine import (
    CalendarNotLoadedError,
    TaxCalendarEngine,
    format_limit_date,
    iva_obligation_for_month,
    today_bogota,
)

router = APIRouter(prefix="/api/iva", tags=["IVA"])


@router.get("/{business_id}", response_model=IvaDetailResponse)
def get_iva_detail(
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    db: Session = Depends(get_db),
):
    """Retorna los periodos fiscales de IVA con métricas para el gráfico circular Ring SVG."""
    business, user, _ = business_access
    if business.income_source == INCOME_SOURCE_MANUAL_SALES:
        raise HTTPException(status_code=404, detail="Este servicio no aplica a tu tipo de negocio.")

    today = today_bogota()
    try:
        obligations = TaxCalendarEngine(db).obligations(business, today)
    except CalendarNotLoadedError:
        obligations = []

    summaries = (
        db.query(MonthlyTaxSummary)
        .filter(MonthlyTaxSummary.business_id == business.id)
        .order_by(MonthlyTaxSummary.period_year_month.desc())
        .all()
    )

    periodos_list: List[IvaPeriodItem] = []

    if summaries:
        for idx, s in enumerate(summaries):
            gen = float(s.iva_generado)
            desc = float(s.iva_descontable)
            saldo = float(s.iva_balance)
            pct = round(min(1.0, desc / gen), 4) if gen > 0 else 0.0

            # Buscar facturas representativas del periodo
            period_invoices = (
                db.query(Invoice)
                .filter(
                    Invoice.business_id == business.id,
                    Invoice.issue_date.like(f"{s.period_year_month}%"),
                )
                .order_by(Invoice.total.desc())
                .limit(5)
                .all()
            )

            facturas_items: List[PeriodInvoiceItem] = [
                PeriodInvoiceItem(
                    fecha=_format_short_date(inv.issue_date),
                    cliente=inv.receiver_name if inv.group_type == "Emitido" else inv.issuer_name,
                    valor=float(inv.total),
                    iva=float(inv.iva),
                    tipo=inv.group_type,
                )
                for inv in period_invoices
            ]

            is_latest = (idx == 0)
            ob = iva_obligation_for_month(obligations, s.period_year_month)
            if ob:
                etiqueta = ob.etiqueta
                limite = format_limit_date(ob.fecha_limite)
                dias = ob.dias
                estado = "presentado" if ob.estado == "completado" else "en_curso"
            else:
                etiqueta = f"Periodo {s.period_year_month}"
                limite = None
                dias = None
                estado = "en_curso" if is_latest else "presentado"

            periodos_list.append(
                IvaPeriodItem(
                    period_key=s.period_year_month,
                    etiqueta=etiqueta,
                    generado=gen,
                    descontable=desc,
                    saldo=saldo,
                    pct=pct,
                    estado=estado,
                    limite=limite,
                    dias=dias,
                    facturas=facturas_items,
                )
            )

    # Si no hay registros aún en monthly_tax_summaries, calculamos directamente desde invoices
    if not periodos_list:
        emitidas = db.query(Invoice).filter(Invoice.business_id == business.id, Invoice.group_type == "Emitido").all()
        recibidas = db.query(Invoice).filter(Invoice.business_id == business.id, Invoice.group_type == "Recibido").all()

        total_gen = sum(float(i.iva) for i in emitidas)
        total_desc = sum(float(i.iva) for i in recibidas)
        saldo = total_gen - total_desc
        pct = round(min(1.0, total_desc / total_gen), 4) if total_gen > 0 else 0.0

        period_key = today.strftime("%Y-%m")
        ob = iva_obligation_for_month(obligations, period_key)
        if ob:
            etiqueta = ob.etiqueta
            limite = format_limit_date(ob.fecha_limite)
            dias = ob.dias
            estado = "presentado" if ob.estado == "completado" else "en_curso"
        else:
            etiqueta = f"Periodo {period_key}"
            limite = None
            dias = None
            estado = "en_curso"

        periodos_list.append(
            IvaPeriodItem(
                period_key=period_key,
                etiqueta=etiqueta,
                generado=total_gen,
                descontable=total_desc,
                saldo=saldo,
                pct=pct,
                estado=estado,
                limite=limite,
                dias=dias,
                facturas=[],
            )
        )

    return IvaDetailResponse(
        business_id=business.id,
        nit=business.nit,
        nombre=business.commercial_name,
        periodos=periodos_list,
    )
