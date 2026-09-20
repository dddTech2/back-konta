"""Servicio de cálculo de ingresos, egresos y utilidad mensual (Story 6.2)."""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Union

from sqlalchemy.orm import Session

from dian_automation.db.models import Business, Invoice, Sale
from dian_automation.extraction.xlsx_parser import is_credit_note

_MONTH_PATTERN = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])$")
_TWO_DECIMALS = Decimal("0.01")


class IncomeError(Exception):
    """Error tipado del servicio de ingresos y egresos."""

    INVALID_MONTH = "INVALID_MONTH"

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_month(month: str) -> tuple[int, int]:
    """Valida el formato YYYY-MM y devuelve (year, month_num) o lanza IncomeError."""
    if not isinstance(month, str) or not _MONTH_PATTERN.fullmatch(month):
        raise IncomeError(
            IncomeError.INVALID_MONTH,
            f"Formato de mes inválido: '{month}'. Se espera formato 'YYYY-MM' con mes entre 01 y 12.",
        )
    match = _MONTH_PATTERN.fullmatch(month)
    return int(match.group(1)), int(match.group(2))


def _month_range(year: int, month_num: int) -> tuple[date, date, datetime, datetime]:
    """Calcula las fechas y datetimes de inicio (inclusivo) y fin (exclusivo) del mes."""
    start_date = date(year, month_num, 1)
    start_dt = datetime(year, month_num, 1, 0, 0, 0)
    if month_num == 12:
        end_date = date(year + 1, 1, 1)
        end_dt = datetime(year + 1, 1, 1, 0, 0, 0)
    else:
        end_date = date(year, month_num + 1, 1)
        end_dt = datetime(year, month_num + 1, 1, 0, 0, 0)
    return start_date, end_date, start_dt, end_dt


def summary(db: Session, business: Union[Business, Any], month: str) -> Dict[str, Any]:
    """Calcula el resumen de ingresos, egresos y utilidad para un negocio en un mes específico.

    - month: string 'YYYY-MM' (01-12).
    - ingresos: suma de Sale.total_amount con sale_date en el mes (solo ventas no anuladas).
    - egresos: suma de Invoice.total con group_type == 'Recibido' e issue_date en el mes,
      multiplicando por -1 las notas de crédito. Las facturas Emitidas no cuentan.
    - utilidad: ingresos - egresos.
    - Mes sin datos devuelve Decimal('0.00') en los tres valores.
    """
    year, month_num = _parse_month(month)
    start_date, end_date, start_dt, end_dt = _month_range(year, month_num)

    business_id = business.id if hasattr(business, "id") else business

    # Ingresos: ventas declaradas del mes (solo ventas no anuladas)
    sales = (
        db.query(Sale.total_amount)
        .filter(
            Sale.business_id == business_id,
            Sale.sale_date >= start_date,
            Sale.sale_date < end_date,
            Sale.voided_at.is_(None),
        )
        .all()
    )
    ingresos = sum((row[0] for row in sales), Decimal("0.00")).quantize(_TWO_DECIMALS)

    # Egresos: facturas recibidas del mes (las notas de crédito restan; Emitidas no cuentan)
    invoices = (
        db.query(Invoice.total, Invoice.document_type)
        .filter(
            Invoice.business_id == business_id,
            Invoice.group_type == "Recibido",
            Invoice.issue_date >= start_dt,
            Invoice.issue_date < end_dt,
        )
        .all()
    )

    egresos = Decimal("0.00")
    for inv_total, doc_type in invoices:
        multiplier = Decimal("-1.00") if is_credit_note(doc_type) else Decimal("1.00")
        egresos += inv_total * multiplier
    egresos = egresos.quantize(_TWO_DECIMALS)

    utilidad = (ingresos - egresos).quantize(_TWO_DECIMALS)

    return {
        "month": month,
        "ingresos": ingresos,
        "egresos": egresos,
        "utilidad": utilidad,
    }


def history(
    db: Session,
    business: Union[Business, Any],
    month: str,
    months: int = 6,
) -> List[Dict[str, Any]]:
    """Devuelve los `months` resúmenes mensuales terminando en `month` (incluido), en orden cronológico ascendente.

    Maneja el cambio de año (ej. 2026-01 retrocede a 2025-12, etc.).
    """
    year, month_num = _parse_month(month)
    if months <= 0:
        return []

    total_months = year * 12 + (month_num - 1)
    month_keys: List[str] = []
    for i in range(months - 1, -1, -1):
        m_idx = total_months - i
        y = m_idx // 12
        m = (m_idx % 12) + 1
        month_keys.append(f"{y:04d}-{m:02d}")

    return [summary(db, business, ym) for ym in month_keys]
