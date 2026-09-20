"""Rutas para la consulta del Historial de Facturas de Kontable."""

from typing import Tuple, Dict, Any, Optional, List
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_

from dian_automation.db.database import get_db
from dian_automation.db.models import Business, User, Invoice
from dian_automation.core.period_utils import period_clause
from dian_automation.api.dependencies import get_business_with_access
from dian_automation.api.schemas import (
    InvoicesListResponse,
    InvoiceDetailItem,
)
from dian_automation.api.routes_dashboard import _format_short_date

router = APIRouter(prefix="/api/invoices", tags=["Facturas"])


@router.get("/{business_id}", response_model=InvoicesListResponse)
def get_invoices(
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    group_type: Optional[str] = Query(None, description="Filtrar por 'Emitido' o 'Recibido'"),
    period: Optional[str] = Query(None, description="Filtrar por periodo YYYY-MM"),
    search: Optional[str] = Query(None, description="Buscar por cliente/proveedor o número"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Retorna el listado paginado de facturas emitidas y recibidas con soporte de búsqueda."""
    business, user, _ = business_access

    query = db.query(Invoice).filter(Invoice.business_id == business.id)

    if group_type and group_type.upper() in ("EMITIDO", "RECIBIDO"):
        gt = "Emitido" if group_type.upper() == "EMITIDO" else "Recibido"
        query = query.filter(Invoice.group_type == gt)

    if period:
        query = query.filter(period_clause(Invoice.issue_date, period))

    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            or_(
                Invoice.receiver_name.ilike(search_pattern),
                Invoice.issuer_name.ilike(search_pattern),
                Invoice.folio.ilike(search_pattern),
                Invoice.prefix.ilike(search_pattern),
            )
        )

    total_count = query.count()
    invoices_db = (
        query.order_by(Invoice.issue_date.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items: List[InvoiceDetailItem] = []
    for inv in invoices_db:
        num_str = f"{inv.prefix or 'FE'}-{inv.folio}" if inv.folio else (inv.cufe[:8] if inv.cufe else "FE")
        is_emitido = (inv.group_type == "Emitido")
        cliente_str = inv.receiver_name if is_emitido else inv.issuer_name
        nit_str = inv.receiver_nit if is_emitido else inv.issuer_nit

        items.append(
            InvoiceDetailItem(
                id=inv.id,
                cufe=inv.cufe,
                num=num_str,
                issue_date=inv.issue_date.isoformat() if inv.issue_date else "",
                fecha_corta=_format_short_date(inv.issue_date),
                cliente=cliente_str or "Consumidor final",
                nit_contraparte=nit_str or "",
                valor=float(inv.total),
                iva=float(inv.iva),
                tipo_documento=inv.document_type,
                group_type=inv.group_type,
            )
        )

    return InvoicesListResponse(
        total_count=total_count,
        invoices=items,
        limit=limit,
        offset=offset,
    )
