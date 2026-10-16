"""Endpoints de administración para contabilidad y reparto de ingresos (Story 10.3)."""

from decimal import Decimal
from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from dian_automation.api.dependencies import get_current_admin
from dian_automation.api.schemas import (
    AccountingClientsSummary,
    AccountingCollectedSummary,
    AccountingMonthlyItem,
    AccountingMrrSummary,
    AccountingPackageCollectedItem,
    AccountingPaymentItem,
    AccountingSplitParty,
    AccountingSplitSummary,
    AccountingSummaryResponse,
    RevenueSplitSettingItem,
    RevenueSplitUpdateRequest,
)
from dian_automation.core.accounting_service import (
    AccountingError,
    accounting_summary,
    ensure_default_split,
    get_split,
    set_split,
)
from dian_automation.core.admin_service import AdminServiceError
from dian_automation.db.database import get_db
from dian_automation.db.models import PACKAGE_CON_IVA, PACKAGE_SIN_IVA, PACKAGES, User

router = APIRouter(prefix="/accounting", tags=["Admin Accounting"])


@router.get("", response_model=AccountingSummaryResponse)
def get_accounting(
    year: Optional[int] = Query(None, description="Año obligatorio (2000-2100)"),
    month: Optional[int] = Query(None, description="Mes opcional (1-12)"),
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Resumen contable y de reparto comercial de Konta (Story 10.3 - AC #8)."""
    if year is None:
        raise AdminServiceError(
            "INVALID_PERIOD",
            "El parámetro 'year' es obligatorio.",
            status_code=422,
        )
    if not (2000 <= year <= 2100):
        raise AdminServiceError(
            "INVALID_PERIOD",
            f"Año '{year}' fuera de rango (2000–2100).",
            status_code=422,
        )
    if month is not None and not (1 <= month <= 12):
        raise AdminServiceError(
            "INVALID_PERIOD",
            f"Mes '{month}' fuera de rango (1–12).",
            status_code=422,
        )

    try:
        data = accounting_summary(db=db, year=year, month=month)
    except AccountingError as ae:
        status_code = 422 if ae.code == "INVALID_PERIOD" else ae.status_code
        raise AdminServiceError(ae.code, ae.message, status_code=status_code) from ae

    by_pkg_collected = {
        pkg: AccountingPackageCollectedItem(
            total=f"{Decimal(str(val['total'])):.2f}",
            count=int(val["count"]),
        )
        for pkg, val in data["collected"]["by_package"].items()
    }
    collected_resp = AccountingCollectedSummary(
        total=f"{Decimal(str(data['collected']['total'])):.2f}",
        count=int(data["collected"]["count"]),
        by_package=by_pkg_collected,
    )

    split_resp = AccountingSplitSummary(
        nyo=AccountingSplitParty(
            total=f"{Decimal(str(data['split']['nyo']['total'])):.2f}",
            by_package={
                pkg: f"{Decimal(str(val)):.2f}"
                for pkg, val in data["split"]["nyo"]["by_package"].items()
            },
        ),
        visioncontable=AccountingSplitParty(
            total=f"{Decimal(str(data['split']['visioncontable']['total'])):.2f}",
            by_package={
                pkg: f"{Decimal(str(val)):.2f}"
                for pkg, val in data["split"]["visioncontable"]["by_package"].items()
            },
        ),
    )

    clients_resp = AccountingClientsSummary(
        active_total=int(data["clients"]["active_total"]),
        by_package={pkg: int(cnt) for pkg, cnt in data["clients"]["by_package"].items()},
        by_income_source={src: int(cnt) for src, cnt in data["clients"]["by_income_source"].items()},
        new_in_period=int(data["clients"]["new_in_period"]),
    )

    mrr_resp = AccountingMrrSummary(
        total=f"{Decimal(str(data['mrr']['total'])):.2f}",
        by_package={
            pkg: f"{Decimal(str(val)):.2f}"
            for pkg, val in data["mrr"]["by_package"].items()
        },
    )

    mrr_split_resp = AccountingSplitSummary(
        nyo=AccountingSplitParty(
            total=f"{Decimal(str(data['mrr_split']['nyo']['total'])):.2f}",
            by_package={
                pkg: f"{Decimal(str(val)):.2f}"
                for pkg, val in data["mrr_split"]["nyo"]["by_package"].items()
            },
        ),
        visioncontable=AccountingSplitParty(
            total=f"{Decimal(str(data['mrr_split']['visioncontable']['total'])):.2f}",
            by_package={
                pkg: f"{Decimal(str(val)):.2f}"
                for pkg, val in data["mrr_split"]["visioncontable"]["by_package"].items()
            },
        ),
    )

    monthly_resp = [
        AccountingMonthlyItem(
            month=str(m["month"]),
            CON_IVA=f"{Decimal(str(m.get(PACKAGE_CON_IVA, 0))):.2f}",
            SIN_IVA=f"{Decimal(str(m.get(PACKAGE_SIN_IVA, 0))):.2f}",
            total=f"{Decimal(str(m.get('total', 0))):.2f}",
        )
        for m in data["monthly"]
    ]

    payments_resp = [
        AccountingPaymentItem(
            payment_date=p["payment_date"].isoformat() if hasattr(p["payment_date"], "isoformat") else str(p["payment_date"]),
            business_id=str(p.get("business_id") or ""),
            commercial_name=str(p.get("commercial_name") or ""),
            package=str(p.get("package") or ""),
            amount=f"{Decimal(str(p['amount'])):.2f}",
            nyo_amount=f"{Decimal(str(p['nyo_amount'])):.2f}",
            visioncontable_amount=f"{Decimal(str(p['visioncontable_amount'])):.2f}",
        )
        for p in data["payments"]
    ]

    return AccountingSummaryResponse(
        collected=collected_resp,
        split=split_resp,
        clients=clients_resp,
        mrr=mrr_resp,
        mrr_split=mrr_split_resp,
        monthly=monthly_resp,
        payments=payments_resp,
    )


@router.get("/split", response_model=List[RevenueSplitSettingItem])
def get_revenue_split(
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Consulta la configuración de reparto vigente por paquete en orden CON_IVA, SIN_IVA (Story 10.3 - AC #9)."""
    ensure_default_split(db)
    rows = get_split(db)
    order_map = {PACKAGE_CON_IVA: 0, PACKAGE_SIN_IVA: 1}
    sorted_rows = sorted(rows, key=lambda r: order_map.get(r.package, 99))
    return [
        RevenueSplitSettingItem(
            package=r.package,
            nyo_pct=f"{r.nyo_pct:.2f}",
            visioncontable_pct=f"{r.visioncontable_pct:.2f}",
            updated_at=r.updated_at.isoformat() if r.updated_at else None,
        )
        for r in sorted_rows
    ]


@router.put("/split/{package}", response_model=RevenueSplitSettingItem)
def update_revenue_split(
    package: str,
    body: RevenueSplitUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Actualiza el porcentaje de reparto de un paquete (Story 10.3 - AC #9)."""
    pkg_clean = (package or "").strip().upper()
    if pkg_clean not in PACKAGES:
        raise AdminServiceError(
            "INVALID_PACKAGE",
            f"Paquete '{package}' no válido. Opciones: {', '.join(PACKAGES)}",
            status_code=404,
        )

    if body.nyo_pct is None:
        raise AdminServiceError(
            "INVALID_SPLIT",
            "El porcentaje 'nyo_pct' es obligatorio.",
            status_code=422,
        )

    try:
        pct = Decimal(str(body.nyo_pct))
    except Exception:
        raise AdminServiceError(
            "INVALID_SPLIT",
            f"Porcentaje '{body.nyo_pct}' inválido.",
            status_code=422,
        )

    if pct < Decimal("0") or pct > Decimal("100"):
        raise AdminServiceError(
            "INVALID_SPLIT",
            "El porcentaje debe estar entre 0 y 100.",
            status_code=422,
        )

    if pct.quantize(Decimal("0.01")) != pct:
        raise AdminServiceError(
            "INVALID_SPLIT",
            "El porcentaje no puede tener más de 2 decimales.",
            status_code=422,
        )

    try:
        setting = set_split(db=db, package=pkg_clean, nyo_pct=pct, admin=admin)
        db.commit()
        db.refresh(setting)
    except AccountingError as ae:
        db.rollback()
        status_code = 404 if ae.code == "INVALID_PACKAGE" else 422
        raise AdminServiceError(ae.code, ae.message, status_code=status_code) from ae

    return RevenueSplitSettingItem(
        package=setting.package,
        nyo_pct=f"{setting.nyo_pct:.2f}",
        visioncontable_pct=f"{setting.visioncontable_pct:.2f}",
        updated_at=setting.updated_at.isoformat() if setting.updated_at else None,
    )
