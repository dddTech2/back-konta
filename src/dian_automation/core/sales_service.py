"""Servicio de dominio compartido para registrar ventas (Telegram y Web).

Concentra la validación del total y la descripción y la persistencia de `Sale`. Los canales
(bot de clientes, endpoint web de la Story 5.3) solo entregan el texto crudo y el cliente ya
autenticado; el canal de origen se distingue con `recorded_via`.
"""

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from dian_automation.core import income_service
from dian_automation.db.models import Business, INCOME_SOURCE_MANUAL_SALES, Sale, User
from dian_automation.subscriptions.lockout_service import SubscriptionLockoutService

RECORDED_VIA_TELEGRAM = "TELEGRAM"
RECORDED_VIA_WEB = "WEB"

MAX_DESCRIPTION_LENGTH = 500

# Dígitos con `.` decimal opcional (máx. 2 decimales). Se acepta un `-` inicial solo para
# distinguir "no positivo" de "formato inválido". Hasta 12 enteros: cabe en NUMERIC(14,2).
_TOTAL_PATTERN = re.compile(r"-?[0-9]{1,12}(?:\.[0-9]{1,2})?")


_INVALID_TOTAL_MESSAGE = (
    "El total debe ser un número con punto decimal opcional (máximo 2 decimales), "
    "sin símbolos ni separadores de miles."
)

# `adjusted()` de un total válido: de 0.01 (-2) a 999999999999.99 (11). Fuera de ese rango se rechaza
# antes de `format(..., "f")`, que con exponentes enormes ("1E+99999999") reserva cientos de MB.
_MIN_ADJUSTED = -2
_MAX_ADJUSTED = 11


class SalesError(Exception):
    """Error tipado del registro de ventas; `code` permite a cada canal elegir su mensaje."""

    INVALID_TOTAL = "INVALID_TOTAL"
    NON_POSITIVE_TOTAL = "NON_POSITIVE_TOTAL"
    DESCRIPTION_TOO_LONG = "DESCRIPTION_TOO_LONG"
    BUSINESS_BLOCKED = "BUSINESS_BLOCKED"
    NOT_MANUAL_SALES = "NOT_MANUAL_SALES"
    SALE_NOT_FOUND = "SALE_NOT_FOUND"
    ALREADY_VOIDED = "ALREADY_VOIDED"

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_total(raw_total: Union[str, int, Decimal, None]) -> Decimal:
    """Valida el total crudo y lo devuelve como Decimal > 0 con a lo sumo 2 decimales."""
    if isinstance(raw_total, Decimal):
        if not raw_total.is_finite() or not _MIN_ADJUSTED <= raw_total.adjusted() <= _MAX_ADJUSTED:
            raise SalesError(SalesError.INVALID_TOTAL, _INVALID_TOTAL_MESSAGE)
        text = format(raw_total, "f")
    elif isinstance(raw_total, (str, int)) and not isinstance(raw_total, bool):
        text = str(raw_total).strip()
    else:
        text = ""

    if not _TOTAL_PATTERN.fullmatch(text):
        raise SalesError(SalesError.INVALID_TOTAL, _INVALID_TOTAL_MESSAGE)

    total = Decimal(text)
    if total <= 0:
        raise SalesError(SalesError.NON_POSITIVE_TOTAL, "El total debe ser mayor a cero.")
    return total


def normalize_description(description: Optional[str]) -> Optional[str]:
    """Recorta espacios en los extremos; vacía se guarda como NULL; máximo 500 caracteres."""
    if description is None:
        return None
    clean = description.strip()
    if not clean:
        return None
    if len(clean) > MAX_DESCRIPTION_LENGTH:
        raise SalesError(
            SalesError.DESCRIPTION_TOO_LONG,
            f"La descripción no puede superar {MAX_DESCRIPTION_LENGTH} caracteres.",
        )
    return clean


def register_sale(
    db: Session,
    *,
    user: User,
    business: Business,
    total: Union[str, int, Decimal, None],
    description: Optional[str] = None,
    recorded_via: str,
    now: Optional[datetime] = None,
) -> Sale:
    """Valida y persiste una venta del cliente autenticado.

    `user` y `business` los entrega el guardia de cada canal (nunca ids arbitrarios). Lanza
    `SalesError` si el total o la descripción no son válidos o si la suscripción está BLOQUEADA;
    en ese caso no se inserta ninguna fila.
    """
    if SubscriptionLockoutService.is_client_blocked(db, user.id):
        raise SalesError(
            SalesError.BUSINESS_BLOCKED,
            "Suscripción suspendida por pago pendiente: no se pueden registrar ventas.",
        )

    if business.income_source != INCOME_SOURCE_MANUAL_SALES:
        raise SalesError(
            SalesError.NOT_MANUAL_SALES,
            "Tu negocio factura electrónicamente: las ventas salen de la DIAN y no se registran a mano.",
        )

    total_amount = parse_total(total)
    clean_description = normalize_description(description)

    if now is None:
        now = datetime.utcnow()

    sale_date = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Bogota")).date()

    sale = Sale(
        business_id=business.id,
        total_amount=total_amount,
        description=clean_description,
        recorded_via=recorded_via,
        recorded_by_user_id=user.id,
        sale_date=sale_date,
        created_at=now,
    )
    db.add(sale)
    db.commit()
    db.refresh(sale)
    return sale


def list_recent_sales(db: Session, business: Business, limit: int = 10) -> List[Sale]:
    """Últimas `limit` ventas NO anuladas del negocio, de la más reciente a la más antigua (created_at desc, id desc)."""
    return (
        db.query(Sale)
        .filter(
            Sale.business_id == business.id,
            Sale.voided_at.is_(None),
        )
        .order_by(Sale.created_at.desc(), Sale.id.desc())
        .limit(limit)
        .all()
    )


def list_recent_sales_numbered(db: Session, business: Business, limit: int = 10) -> List[Tuple[int, Sale]]:
    """Últimas `limit` ventas NO anuladas, de la más reciente a la más antigua, cada una con su número estable.
    El número de una venta es su posición 1-based entre TODAS las ventas del negocio (anuladas incluidas) ordenadas por (created_at, id):
    no cambia al registrar ni anular otras ventas."""
    recent_sales = list_recent_sales(db, business, limit=limit)
    numbered = []
    for s in recent_sales:
        count = (
            db.query(func.count(Sale.id))
            .filter(
                Sale.business_id == business.id,
                or_(
                    Sale.created_at < s.created_at,
                    and_(Sale.created_at == s.created_at, Sale.id <= s.id),
                ),
            )
            .scalar()
        )
        numbered.append((int(count or 0), s))
    return numbered


def find_recent_sale_by_number(db: Session, business: Business, number: int, limit: int = 10) -> Optional[Sale]:
    """La venta con ese número estable SOLO si está entre las últimas `limit` no anuladas; None en otro caso."""
    if number <= 0:
        return None
    for num, sale in list_recent_sales_numbered(db, business, limit=limit):
        if num == number:
            return sale
    return None


def list_month_sales(db: Session, business: Business, month: str, limit: int = 100) -> List[Sale]:
    """Ventas NO anuladas del negocio con sale_date dentro del mes 'YYYY-MM', orden sale_date desc, created_at desc, id desc.
    Mes inválido -> lanza income_service.IncomeError(INVALID_MONTH) (reutiliza income_service._parse_month y _month_range)."""
    year, month_num = income_service._parse_month(month)
    start_date, end_date, _, _ = income_service._month_range(year, month_num)
    return (
        db.query(Sale)
        .filter(
            Sale.business_id == business.id,
            Sale.sale_date >= start_date,
            Sale.sale_date < end_date,
            Sale.voided_at.is_(None),
        )
        .order_by(Sale.sale_date.desc(), Sale.created_at.desc(), Sale.id.desc())
        .limit(limit)
        .all()
    )


def void_sale(db: Session, *, user: User, business: Business, sale_id: str, now: Optional[datetime] = None) -> Sale:
    """Anula una venta del negocio. `now` es UTC naive (por defecto datetime.utcnow())."""
    if SubscriptionLockoutService.is_client_blocked(db, user.id):
        raise SalesError(
            SalesError.BUSINESS_BLOCKED,
            "Suscripción suspendida por pago pendiente: no se pueden anular ventas.",
        )

    if business.income_source != INCOME_SOURCE_MANUAL_SALES:
        raise SalesError(
            SalesError.NOT_MANUAL_SALES,
            "Tu negocio factura electrónicamente: las ventas salen de la DIAN y no se registran a mano.",
        )

    sale = (
        db.query(Sale)
        .filter(Sale.id == sale_id, Sale.business_id == business.id)
        .first()
    )
    if not sale:
        raise SalesError(SalesError.SALE_NOT_FOUND, "No encontramos esa venta.")

    if sale.voided_at is not None:
        raise SalesError(SalesError.ALREADY_VOIDED, "Esa venta ya está anulada.")

    if now is None:
        now = datetime.utcnow()

    updated = (
        db.query(Sale)
        .filter(
            Sale.id == sale_id,
            Sale.business_id == business.id,
            Sale.voided_at.is_(None),
        )
        .update(
            {"voided_at": now, "voided_by_user_id": user.id},
            synchronize_session=False,
        )
    )
    if updated == 0:
        db.rollback()
        raise SalesError(SalesError.ALREADY_VOIDED, "Esa venta ya está anulada.")

    db.commit()
    db.refresh(sale)
    return sale
