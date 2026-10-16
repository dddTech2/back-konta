"""Servicio de cálculo de ingresos, egresos y utilidad mensual (Story 6.2)."""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Union

from sqlalchemy.orm import Session

from dian_automation.core.business_profile import has_einvoicing, has_manual_sales
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


def _ingresos_por_perfil(business: Any, emitido: Decimal, ventas: Decimal) -> Decimal:
    """Calcula los ingresos según el perfil de ingresos del negocio (Story 10.4, Q-10b).

    - MANUAL_SALES: ventas (emitido se calcula pero no se suma).
    - MIXED: emitido + ventas (supuesto Q-10b: las ventas registradas son las no facturadas).
    - DIAN: emitido.
    """
    if has_einvoicing(business) and has_manual_sales(business):
        return emitido + ventas
    if has_einvoicing(business):
        return emitido
    return ventas


def summary(db: Session, business: Union[Business, Any], month: str) -> Dict[str, Any]:
    """Calcula el resumen de ingresos, egresos y utilidad para un negocio en un mes específico.

    - month: string 'YYYY-MM' (01-12).
    - emitido: suma de Invoice.total con group_type == 'Emitido' del mes (notas crédito restan).
    - ventas: suma de Sale.total_amount con sale_date en el mes (solo ventas no anuladas).
    - ingresos: calculado por _ingresos_por_perfil según perfil (MANUAL_SALES, MIXED o DIAN).
    - egresos: suma de Invoice.total con group_type == 'Recibido' e issue_date en el mes,
      multiplicando por -1 las notas de crédito.
    - utilidad: ingresos - egresos.
    - diferencia: emitido - ventas.
    - emitido_pct y ventas_pct: participación sobre ingresos (2 decimales, Decimal('0.00') si ingresos es 0).
    - emitido_count: facturas emitidas del mes sin notas crédito.
    - ventas_count: ventas no anuladas del mes.
    - clientes_count: receiver_nit distintos de facturas emitidas del mes sin notas crédito.
    """
    year, month_num = _parse_month(month)
    start_date, end_date, start_dt, end_dt = _month_range(year, month_num)

    if hasattr(business, "id"):
        business_obj = business
        business_id = business.id
    elif isinstance(business, dict) and "id" in business:
        business_obj = business
        business_id = business["id"]
    else:
        business_id = business
        business_obj = db.get(Business, business_id) if business_id else None

    # Ventas declaradas del mes (solo ventas no anuladas)
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
    ventas = sum((row[0] for row in sales), Decimal("0.00")).quantize(_TWO_DECIMALS)
    ventas_count = len(sales)

    # Facturas emitidas del mes (las notas de crédito restan; sin notas de crédito para conteos)
    emitted_invoices = (
        db.query(Invoice.total, Invoice.document_type, Invoice.receiver_nit)
        .filter(
            Invoice.business_id == business_id,
            Invoice.group_type == "Emitido",
            Invoice.issue_date >= start_dt,
            Invoice.issue_date < end_dt,
        )
        .all()
    )

    emitido = Decimal("0.00")
    emitido_count = 0
    clientes_set = set()

    for inv_total, doc_type, receiver_nit in emitted_invoices:
        if is_credit_note(doc_type):
            emitido -= inv_total
        else:
            emitido += inv_total
            emitido_count += 1
            if receiver_nit:
                clientes_set.add(receiver_nit)

    emitido = emitido.quantize(_TWO_DECIMALS)
    clientes_count = len(clientes_set)

    # Egresos: facturas recibidas del mes (las notas de crédito restan; Emitidas no cuentan)
    received_invoices = (
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
    for inv_total, doc_type in received_invoices:
        multiplier = Decimal("-1.00") if is_credit_note(doc_type) else Decimal("1.00")
        egresos += inv_total * multiplier
    egresos = egresos.quantize(_TWO_DECIMALS)

    # Ingresos y utilidad según perfil de negocio
    ingresos = _ingresos_por_perfil(business_obj, emitido, ventas).quantize(_TWO_DECIMALS)
    utilidad = (ingresos - egresos).quantize(_TWO_DECIMALS)

    # Métricas de contraste y participación
    diferencia = (emitido - ventas).quantize(_TWO_DECIMALS)

    if ingresos <= Decimal("0.00"):
        emitido_pct = Decimal("0.00")
        ventas_pct = Decimal("0.00")
    else:
        emitido_pct = ((emitido / ingresos) * Decimal("100")).quantize(_TWO_DECIMALS)
        ventas_pct = ((ventas / ingresos) * Decimal("100")).quantize(_TWO_DECIMALS)

    return {
        "month": month,
        "ingresos": ingresos,
        "egresos": egresos,
        "utilidad": utilidad,
        "emitido": emitido,
        "ventas": ventas,
        "diferencia": diferencia,
        "emitido_pct": emitido_pct,
        "ventas_pct": ventas_pct,
        "emitido_count": emitido_count,
        "ventas_count": ventas_count,
        "clientes_count": clientes_count,
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

    if hasattr(business, "id"):
        business_obj = business
    elif isinstance(business, dict) and "id" in business:
        business_obj = business
    else:
        business_obj = db.get(Business, business) if business else None

    total_months = year * 12 + (month_num - 1)
    month_keys: List[str] = []
    for i in range(months - 1, -1, -1):
        m_idx = total_months - i
        y = m_idx // 12
        m = (m_idx % 12) + 1
        month_keys.append(f"{y:04d}-{m:02d}")

    target_business = business_obj if business_obj is not None else business
    return [summary(db, target_business, ym) for ym in month_keys]
