"""Rutas para la consulta del calendario tributario DIAN (Story 4.1b)."""

from typing import Any, Dict, Tuple

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from dian_automation.api.dependencies import get_business_with_access
from dian_automation.api.schemas import CalendarObligation, CalendarResponse
from dian_automation.core.calendar_engine import (
    CalendarNotLoadedError,
    TaxCalendarEngine,
    today_bogota,
)
from dian_automation.db.database import get_db
from dian_automation.db.models import Business, INCOME_SOURCE_MANUAL_SALES, User

router = APIRouter(prefix="/api/calendar", tags=["Calendario"])


@router.get("/{business_id}", response_model=CalendarResponse)
def get_tax_calendar(
    business_id: str,
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    db: Session = Depends(get_db),
) -> CalendarResponse:
    """Retorna las obligaciones tributarias aplicables al negocio según su perfil."""
    business, user, _ = business_access

    if business.income_source == INCOME_SOURCE_MANUAL_SALES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Este servicio no aplica a tu tipo de negocio.",
        )

    try:
        engine = TaxCalendarEngine(db)
        obligations = engine.obligations(business, today_bogota())
    except CalendarNotLoadedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    return CalendarResponse(
        obligaciones=[
            CalendarObligation(
                tax_type=ob.tax_type,
                etiqueta=ob.etiqueta,
                fecha_limite=ob.fecha_limite.isoformat(),
                estado=ob.estado,
                dias=ob.dias,
            )
            for ob in obligations
        ]
    )
