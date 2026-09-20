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
from dian_automation.db.models import Business, INCOME_SOURCE_MANUAL_SALES, User

router = APIRouter(prefix="/api/income-summary", tags=["Ingresos"])

_BOGOTA_TZ = ZoneInfo("America/Bogota")


def _format_decimal(val: Any) -> str:
    """Convierte Decimal ya cuantizado o valor numérico a cadena con 2 decimales."""
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
    """Retorna el resumen de ingresos, egresos y utilidad para un negocio MANUAL_SALES."""
    business, user, _ = business_access

    if business.income_source != INCOME_SOURCE_MANUAL_SALES:
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
        historial=[
            IncomeSummaryItem(
                month=item["month"],
                ingresos=_format_decimal(item["ingresos"]),
                egresos=_format_decimal(item["egresos"]),
                utilidad=_format_decimal(item["utilidad"]),
            )
            for item in hist
        ],
    )
