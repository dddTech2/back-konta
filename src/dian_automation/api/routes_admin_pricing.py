"""Endpoints de administración de tarifas, planes y configuración de precios (Story 9.2)."""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from dian_automation.api.dependencies import get_current_admin
from dian_automation.api.schemas import (
    BillingSettingsResponse,
    BillingSettingsUpdateRequest,
    PricingOverviewResponse,
    PricingPlanCreateRequest,
    PricingPlanItem,
    PricingPlanUpdateRequest,
    PricingQuoteDetailResponse,
    PricingQuoteItem,
    PricingRateCreateRequest,
    PricingRateHistoryItem,
    PricingRateItem,
    PricingRateResponse,
    PricingScheduledRateItem,
)
from dian_automation.core.admin_service import (
    INCOME_SOURCE_BY_TIPO,
    AdminServiceError,
)
from dian_automation.db.database import get_db
from dian_automation.db.models import (
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    INCOME_SOURCES,
    TAXPAYER_TYPE_PERSONA_JURIDICA,
    TAXPAYER_TYPE_PERSONA_NATURAL,
    TAXPAYER_TYPES,
    PricingPlan,
    PricingRate,
    User,
    generate_uuid,
)
from dian_automation.subscriptions.pricing import (
    MAX_MONTHLY_PRICE,
    PricingError,
    PricingService,
    ensure_default_pricing,
)

router = APIRouter(prefix="/pricing", tags=["Admin Pricing"])


@router.get("", response_model=PricingOverviewResponse)
def get_pricing_overview(
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Consulta consolidada de tarifas, planes y cotizaciones activas (Story 9.2 - AC #1)."""
    ensure_default_pricing(db)
    today_bogota = datetime.now(ZoneInfo("America/Bogota")).date()
    grace_days = PricingService.get_grace_days(db)

    plans_models = PricingService.list_plans(db)
    plans = [
        PricingPlanItem(
            code=p.code,
            name=p.name,
            months=p.months,
            discount_rate=f"{p.discount_rate:.2f}",
            is_active=p.is_active,
            sort_order=p.sort_order,
        )
        for p in plans_models
    ]

    segments = [
        (INCOME_SOURCE_DIAN, TAXPAYER_TYPE_PERSONA_NATURAL),
        (INCOME_SOURCE_DIAN, TAXPAYER_TYPE_PERSONA_JURIDICA),
        (INCOME_SOURCE_MANUAL_SALES, TAXPAYER_TYPE_PERSONA_NATURAL),
        (INCOME_SOURCE_MANUAL_SALES, TAXPAYER_TYPE_PERSONA_JURIDICA),
    ]

    rates = []
    for inc, tx in segments:
        r = PricingService.current_rate(db, inc, tx, on=today_bogota)
        rates.append(
            PricingRateItem(
                income_source=r.income_source,
                taxpayer_type=r.taxpayer_type,
                monthly_price=f"{r.monthly_price:.2f}",
                effective_from=r.effective_from.isoformat(),
            )
        )

    scheduled_models = (
        db.query(PricingRate)
        .filter(PricingRate.effective_from > today_bogota)
        .order_by(PricingRate.effective_from.asc(), PricingRate.created_at.asc())
        .all()
    )
    scheduled = [
        PricingScheduledRateItem(
            id=sr.id,
            income_source=sr.income_source,
            taxpayer_type=sr.taxpayer_type,
            monthly_price=f"{sr.monthly_price:.2f}",
            effective_from=sr.effective_from.isoformat(),
        )
        for sr in scheduled_models
    ]

    quotes = []
    active_plans = [p for p in plans_models if p.is_active]
    for inc, tx in segments:
        for p in active_plans:
            q = PricingService.quote_for_segment(db, inc, tx, p.code, start_date=today_bogota)
            quotes.append(
                PricingQuoteItem(
                    income_source=inc,
                    taxpayer_type=tx,
                    plan=p.code,
                    months=q.months,
                    monthly_price=f"{q.monthly_price:.2f}",
                    gross_total=f"{q.gross_total:.2f}",
                    discount_rate=f"{q.discount_rate:.2f}",
                    discount_amount=f"{q.discount_amount:.2f}",
                    final_price=f"{q.final_price:.2f}",
                )
            )

    return PricingOverviewResponse(
        grace_days=grace_days,
        plans=plans,
        rates=rates,
        scheduled=scheduled,
        quotes=quotes,
    )


@router.put("/plans/{code}", response_model=PricingPlanItem)
def update_plan(
    code: str,
    body: PricingPlanUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Actualiza nombre, descuento o estado activo de un plan (Story 9.2 - AC #2)."""
    ensure_default_pricing(db)
    if body.months is not None:
        raise AdminServiceError(
            "INVALID_PLAN_MONTHS",
            "La duración de un plan no se cambia; crea un plan nuevo",
            status_code=422,
        )

    plan_code = (code or "").upper().strip()
    existing = db.query(PricingPlan).filter(PricingPlan.code == plan_code).first()
    if not existing:
        raise AdminServiceError("PLAN_NOT_FOUND", f"Plan '{code}' no existe.", status_code=404)

    if body.discount_rate is not None:
        try:
            disc_val = Decimal(str(body.discount_rate))
        except Exception:
            raise AdminServiceError("INVALID_DISCOUNT_RATE", "El descuento debe ser numérico.", status_code=422)
        if not (Decimal("0.00") <= disc_val <= Decimal("50.00")):
            raise AdminServiceError("INVALID_DISCOUNT_RATE", "El descuento debe estar entre 0% y 50%.", status_code=422)

    try:
        updated = PricingService.update_plan(
            db=db,
            code=plan_code,
            name=body.name,
            discount_rate=body.discount_rate,
            is_active=body.is_active,
            admin=admin,
        )
    except PricingError as pe:
        raise AdminServiceError(pe.code, pe.message, status_code=pe.status_code) from pe

    return PricingPlanItem(
        code=updated.code,
        name=updated.name,
        months=updated.months,
        discount_rate=f"{updated.discount_rate:.2f}",
        is_active=updated.is_active,
        sort_order=updated.sort_order,
    )


@router.post("/plans", response_model=PricingPlanItem, status_code=status.HTTP_201_CREATED)
def create_plan(
    body: PricingPlanCreateRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Crea un nuevo plan de suscripción (Story 9.2 - AC #2b)."""
    ensure_default_pricing(db)
    code_clean = (body.code or "").strip().upper()
    if not re.match(r"^[A-Z][A-Z_]{1,19}$", code_clean):
        raise AdminServiceError(
            "INVALID_PLAN_CODE",
            "El código del plan debe tener entre 2 y 20 caracteres (mayúsculas y guiones bajos).",
            status_code=422,
        )

    name_clean = (body.name or "").strip()
    if not (1 <= len(name_clean) <= 60):
        raise AdminServiceError(
            "INVALID_PLAN_NAME",
            "El nombre visible del plan debe tener entre 1 y 60 caracteres.",
            status_code=422,
        )

    try:
        months_val = int(body.months)
    except (ValueError, TypeError):
        raise AdminServiceError("INVALID_PLAN_MONTHS", "La duración en meses debe ser un entero.", status_code=422)

    if not (1 <= months_val <= 36):
        raise AdminServiceError(
            "INVALID_PLAN_MONTHS",
            "La duración del plan debe estar entre 1 y 36 meses.",
            status_code=422,
        )

    try:
        disc_val = Decimal(str(body.discount_rate))
    except Exception:
        raise AdminServiceError("INVALID_DISCOUNT_RATE", "El descuento debe ser numérico.", status_code=422)

    if not (Decimal("0.00") <= disc_val <= Decimal("50.00")):
        raise AdminServiceError(
            "INVALID_DISCOUNT_RATE",
            "El descuento debe estar entre 0% y 50%.",
            status_code=422,
        )

    try:
        new_plan = PricingService.create_plan(
            db=db,
            code=code_clean,
            name=name_clean,
            months=months_val,
            discount_rate=disc_val,
            admin=admin,
        )
    except PricingError as pe:
        raise AdminServiceError(pe.code, pe.message, status_code=pe.status_code) from pe

    return PricingPlanItem(
        code=new_plan.code,
        name=new_plan.name,
        months=new_plan.months,
        discount_rate=f"{new_plan.discount_rate:.2f}",
        is_active=new_plan.is_active,
        sort_order=new_plan.sort_order,
    )


@router.get("/settings", response_model=BillingSettingsResponse)
def get_billing_settings(
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Consulta la configuración global de días de gracia (Story 9.2 - AC #2c)."""
    ensure_default_pricing(db)
    days = PricingService.get_grace_days(db)
    from dian_automation.db.models import BillingSetting

    setting = db.query(BillingSetting).filter(BillingSetting.id == 1).first()
    return BillingSettingsResponse(
        grace_days=days,
        updated_at=setting.updated_at.isoformat() if setting and setting.updated_at else None,
    )


@router.put("/settings", response_model=BillingSettingsResponse)
def update_billing_settings(
    body: BillingSettingsUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Actualiza los días de gracia globales (1–15) (Story 9.2 - AC #2c)."""
    ensure_default_pricing(db)
    if not (1 <= body.grace_days <= 15):
        raise AdminServiceError(
            "INVALID_GRACE_DAYS",
            "Los días de gracia deben estar entre 1 y 15.",
            status_code=422,
        )

    PricingService.set_grace_days(db, body.grace_days, admin=admin)
    from dian_automation.db.models import BillingSetting

    setting = db.query(BillingSetting).filter(BillingSetting.id == 1).first()
    return BillingSettingsResponse(
        grace_days=setting.grace_days,
        updated_at=setting.updated_at.isoformat() if setting and setting.updated_at else None,
    )


@router.post("/rates", response_model=PricingRateResponse)
def create_or_update_rate(
    body: PricingRateCreateRequest,
    response: Response,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Crea una tarifa mensual o actualiza la del mismo segmento y fecha (Story 9.2 - AC #3)."""
    ensure_default_pricing(db)
    inc_raw = (body.income_source or "").strip().upper()
    if inc_raw in INCOME_SOURCE_BY_TIPO:
        inc_raw = INCOME_SOURCE_BY_TIPO[inc_raw]
    if inc_raw not in INCOME_SOURCES:
        raise AdminServiceError(
            "INVALID_INCOME_SOURCE",
            f"Origen de ingresos '{body.income_source}' inválido. Opciones: {', '.join(INCOME_SOURCES)}",
            status_code=422,
        )

    tx_raw = (body.taxpayer_type or "").strip().upper()
    if tx_raw == "PERSONA":
        tx_raw = TAXPAYER_TYPE_PERSONA_NATURAL
    elif tx_raw == "EMPRESA":
        tx_raw = TAXPAYER_TYPE_PERSONA_JURIDICA
    if tx_raw not in TAXPAYER_TYPES:
        raise AdminServiceError(
            "INVALID_TAXPAYER_TYPE",
            f"Tipo de contribuyente '{body.taxpayer_type}' inválido. Opciones: {', '.join(TAXPAYER_TYPES)}",
            status_code=422,
        )

    try:
        price_val = Decimal(str(body.monthly_price))
    except Exception:
        raise AdminServiceError("INVALID_MONTHLY_PRICE", "El precio mensual debe ser numérico.", status_code=422)

    if not (Decimal("0") < price_val <= MAX_MONTHLY_PRICE):
        raise AdminServiceError(
            "INVALID_MONTHLY_PRICE",
            "El precio mensual debe ser mayor a 0 y hasta $10.000.000.",
            status_code=422,
        )

    today_bogota = datetime.now(ZoneInfo("America/Bogota")).date()
    eff_date = body.effective_from or today_bogota
    if eff_date < today_bogota:
        raise AdminServiceError(
            "INVALID_EFFECTIVE_DATE",
            "La fecha de vigencia no puede ser anterior a hoy.",
            status_code=422,
        )

    existing = (
        db.query(PricingRate)
        .filter(
            PricingRate.income_source == inc_raw,
            PricingRate.taxpayer_type == tx_raw,
            PricingRate.effective_from == eff_date,
        )
        .first()
    )

    if existing:
        existing.monthly_price = price_val
        existing.created_by_admin_id = admin.id if admin else None
        db.commit()
        db.refresh(existing)
        response.status_code = status.HTTP_200_OK
        return PricingRateResponse(
            id=existing.id,
            income_source=existing.income_source,
            taxpayer_type=existing.taxpayer_type,
            monthly_price=f"{existing.monthly_price:.2f}",
            effective_from=existing.effective_from.isoformat(),
            created_by=admin.full_name if admin else None,
        )
    else:
        new_rate = PricingRate(
            id=generate_uuid(),
            income_source=inc_raw,
            taxpayer_type=tx_raw,
            monthly_price=price_val,
            effective_from=eff_date,
            created_by_admin_id=admin.id if admin else None,
            created_at=datetime.utcnow(),
        )
        db.add(new_rate)
        db.commit()
        db.refresh(new_rate)
        response.status_code = status.HTTP_201_CREATED
        return PricingRateResponse(
            id=new_rate.id,
            income_source=new_rate.income_source,
            taxpayer_type=new_rate.taxpayer_type,
            monthly_price=f"{new_rate.monthly_price:.2f}",
            effective_from=new_rate.effective_from.isoformat(),
            created_by=admin.full_name if admin else None,
        )


@router.delete("/rates/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_scheduled_rate(
    id: str,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Elimina una tarifa programada que aún no ha entrado en vigencia (Story 9.2 - AC #4)."""
    ensure_default_pricing(db)
    rate = db.query(PricingRate).filter(PricingRate.id == id).first()
    if not rate:
        raise AdminServiceError("RATE_NOT_FOUND", f"Tarifa '{id}' no encontrada.", status_code=404)

    today_bogota = datetime.now(ZoneInfo("America/Bogota")).date()
    if rate.effective_from <= today_bogota:
        raise AdminServiceError(
            "RATE_ALREADY_EFFECTIVE",
            "La tarifa ya entró en vigencia y no puede ser eliminada.",
            status_code=409,
        )

    db.delete(rate)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/rates/history", response_model=List[PricingRateHistoryItem])
def get_rate_history(
    income_source: str = Query(..., description="Origen de ingresos (DIAN o MANUAL_SALES)"),
    taxpayer_type: str = Query(..., description="Tipo de contribuyente (PERSONA_NATURAL o PERSONA_JURIDICA)"),
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Historial de tarifas de un segmento ordenadas de la más reciente a la más antigua (Story 9.2 - AC #5)."""
    ensure_default_pricing(db)
    inc_raw = (income_source or "").strip().upper()
    if inc_raw in INCOME_SOURCE_BY_TIPO:
        inc_raw = INCOME_SOURCE_BY_TIPO[inc_raw]
    if inc_raw not in INCOME_SOURCES:
        raise AdminServiceError(
            "INVALID_INCOME_SOURCE",
            f"Origen de ingresos '{income_source}' inválido.",
            status_code=422,
        )

    tx_raw = (taxpayer_type or "").strip().upper()
    if tx_raw == "PERSONA":
        tx_raw = TAXPAYER_TYPE_PERSONA_NATURAL
    elif tx_raw == "EMPRESA":
        tx_raw = TAXPAYER_TYPE_PERSONA_JURIDICA
    if tx_raw not in TAXPAYER_TYPES:
        raise AdminServiceError(
            "INVALID_TAXPAYER_TYPE",
            f"Tipo de contribuyente '{taxpayer_type}' inválido.",
            status_code=422,
        )

    rates = (
        db.query(PricingRate)
        .filter(
            PricingRate.income_source == inc_raw,
            PricingRate.taxpayer_type == tx_raw,
        )
        .order_by(PricingRate.effective_from.desc(), PricingRate.created_at.desc())
        .all()
    )

    return [
        PricingRateHistoryItem(
            id=r.id,
            income_source=r.income_source,
            taxpayer_type=r.taxpayer_type,
            monthly_price=f"{r.monthly_price:.2f}",
            effective_from=r.effective_from.isoformat(),
            created_at=r.created_at.isoformat() if r.created_at else None,
            created_by=r.created_by_admin.full_name if r.created_by_admin else None,
        )
        for r in rates
    ]


@router.get("/quote", response_model=PricingQuoteDetailResponse)
def get_quote(
    income_source: str = Query(..., description="Origen de ingresos"),
    taxpayer_type: Optional[str] = Query(None, description="Tipo de contribuyente"),
    person_type: Optional[str] = Query(None, description="PERSONA o EMPRESA"),
    plan: str = Query(..., description="Código del plan"),
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Calcula la cotización para un segmento y plan activo (Story 9.2 - AC #6)."""
    ensure_default_pricing(db)
    inc_raw = (income_source or "").strip().upper()
    if inc_raw in INCOME_SOURCE_BY_TIPO:
        inc_raw = INCOME_SOURCE_BY_TIPO[inc_raw]
    if inc_raw not in INCOME_SOURCES:
        raise AdminServiceError(
            "INVALID_INCOME_SOURCE",
            f"Origen de ingresos '{income_source}' inválido.",
            status_code=422,
        )

    tx_raw = None
    if person_type:
        pt_clean = person_type.strip().upper()
        if pt_clean == "PERSONA":
            tx_raw = TAXPAYER_TYPE_PERSONA_NATURAL
        elif pt_clean == "EMPRESA":
            tx_raw = TAXPAYER_TYPE_PERSONA_JURIDICA
    elif taxpayer_type:
        tt_clean = taxpayer_type.strip().upper()
        if tt_clean in TAXPAYER_TYPES:
            tx_raw = tt_clean
        elif tt_clean == "PERSONA":
            tx_raw = TAXPAYER_TYPE_PERSONA_NATURAL
        elif tt_clean == "EMPRESA":
            tx_raw = TAXPAYER_TYPE_PERSONA_JURIDICA

    if not tx_raw or tx_raw not in TAXPAYER_TYPES:
        raise AdminServiceError(
            "INVALID_TAXPAYER_TYPE",
            f"Tipo de contribuyente '{taxpayer_type or person_type}' inválido.",
            status_code=422,
        )

    plan_code = (plan or "").strip().upper()
    if not plan_code:
        raise AdminServiceError("INVALID_PLAN", "El código del plan es obligatorio.", status_code=400)

    try:
        quote = PricingService.quote_for_segment(
            db=db,
            income_source=inc_raw,
            taxpayer_type=tx_raw,
            plan_code=plan_code,
        )
    except PricingError as pe:
        raise AdminServiceError(pe.code, pe.message, status_code=pe.status_code) from pe

    return PricingQuoteDetailResponse(
        income_source=inc_raw,
        taxpayer_type=tx_raw,
        plan=quote.plan,
        months=quote.months,
        monthly_price=f"{quote.monthly_price:.2f}",
        gross_total=f"{quote.gross_total:.2f}",
        discount_rate=f"{quote.discount_rate:.2f}",
        discount_amount=f"{quote.discount_amount:.2f}",
        final_price=f"{quote.final_price:.2f}",
        start_date=quote.start_date.isoformat(),
        cutoff_date=quote.cutoff_date.isoformat(),
    )
