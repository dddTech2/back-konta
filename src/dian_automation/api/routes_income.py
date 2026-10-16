"""Rutas para el resumen de ingresos, egresos y utilidad mensual (Story 6.3)."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from dian_automation.api.dependencies import get_business_with_access
from dian_automation.api.schemas import IncomeSummaryItem, IncomeSummaryResponse
from dian_automation.core import income_service
from dian_automation.db.database import get_db
from dian_automation.core.business_profile import has_manual_sales
from dian_automation.db.models import Business, User

router = APIRouter(prefix="/api/income-summary", tags=["Ingresos"])

_BOGOTA_TZ = ZoneInfo("America/Bogota")


def _format_decimal(val: Any) -> Optional[str]:
    """Convierte Decimal ya cuantizado o valor numérico a cadena con 2 decimales (None se conserva)."""
    if val is None:
        return None
    if isinstance(val, Decimal):
        return format(val, "f")
    return format(Decimal(str(val)), ".2f")


@router.get("/{business_id}", response_model=IncomeSummaryResponse)
def get_income_summary(
    business_id: str,
    month: Optional[str] = Query(None),
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    db: Session = Depends(get_db),
) -> IncomeSummaryResponse:
    """Retorna el resumen de ingresos, egresos y utilidad para un negocio con ventas manuales."""
    business, user, _ = business_access

    if not has_manual_sales(business):
        raise HTTPException(status_code=404, detail="Este servicio no aplica a tu tipo de negocio.")

    target_month = month
    if not target_month:
        target_month = datetime.now(_BOGOTA_TZ).strftime("%Y-%m")

    try:
        curr = income_service.summary(db, business, target_month)
        hist = income_service.history(db, business, target_month)
    except income_service.IncomeError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc

    return IncomeSummaryResponse(
        month=curr["month"],
        ingresos=_format_decimal(curr["ingresos"]),
        egresos=_format_decimal(curr["egresos"]),
        utilidad=_format_decimal(curr["utilidad"]),
        emitido=_format_decimal(curr.get("emitido", Decimal("0.00"))),
        ventas=_format_decimal(curr.get("ventas", Decimal("0.00"))),
        diferencia=_format_decimal(curr.get("diferencia", Decimal("0.00"))),
        emitido_count=int(curr.get("emitido_count", 0)),
        ventas_count=int(curr.get("ventas_count", 0)),
        clientes_count=int(curr.get("clientes_count", 0)),
        historial=[
            IncomeSummaryItem(
                month=item["month"],
                ingresos=_format_decimal(item["ingresos"]),
                egresos=_format_decimal(item["egresos"]),
                utilidad=_format_decimal(item["utilidad"]),
                emitido=_format_decimal(item.get("emitido", Decimal("0.00"))),
                ventas=_format_decimal(item.get("ventas", Decimal("0.00"))),
            )
            for item in hist
        ],
    )
