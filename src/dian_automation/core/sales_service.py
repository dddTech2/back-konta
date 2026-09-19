"""Servicio de dominio compartido para registrar ventas (Telegram y Web).

Concentra la validación del total y la descripción y la persistencia de `Sale`. Los canales
(bot de clientes, endpoint web de la Story 5.3) solo entregan el texto crudo y el cliente ya
autenticado; el canal de origen se distingue con `recorded_via`.
"""

import re
from decimal import Decimal
from typing import Optional, Union

from sqlalchemy.orm import Session

from dian_automation.db.models import Business, Sale, User
from dian_automation.subscriptions.lockout_service import SubscriptionLockoutService

RECORDED_VIA_TELEGRAM = "TELEGRAM"
RECORDED_VIA_WEB = "WEB"

MAX_DESCRIPTION_LENGTH = 500

# Dígitos con `.` decimal opcional (máx. 2 decimales). Se acepta un `-` inicial solo para
# distinguir "no positivo" de "formato inválido". Hasta 12 enteros: cabe en NUMERIC(14,2).
_TOTAL_PATTERN = re.compile(r"-?[0-9]{1,12}(?:\.[0-9]{1,2})?")


class SalesError(Exception):
    """Error tipado del registro de ventas; `code` permite a cada canal elegir su mensaje."""

    INVALID_TOTAL = "INVALID_TOTAL"
    NON_POSITIVE_TOTAL = "NON_POSITIVE_TOTAL"
    DESCRIPTION_TOO_LONG = "DESCRIPTION_TOO_LONG"
    BUSINESS_BLOCKED = "BUSINESS_BLOCKED"

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_total(raw_total: Union[str, int, Decimal, None]) -> Decimal:
    """Valida el total crudo y lo devuelve como Decimal > 0 con a lo sumo 2 decimales."""
    if isinstance(raw_total, Decimal):
        text = format(raw_total, "f")
    elif isinstance(raw_total, (str, int)) and not isinstance(raw_total, bool):
        text = str(raw_total).strip()
    else:
        text = ""

    if not _TOTAL_PATTERN.fullmatch(text):
        raise SalesError(
            SalesError.INVALID_TOTAL,
            "El total debe ser un número con punto decimal opcional (máximo 2 decimales), "
            "sin símbolos ni separadores de miles.",
        )

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

    total_amount = parse_total(total)
    clean_description = normalize_description(description)

    sale = Sale(
        business_id=business.id,
        total_amount=total_amount,
        description=clean_description,
        recorded_via=recorded_via,
        recorded_by_user_id=user.id,
    )
    db.add(sale)
    db.commit()
    db.refresh(sale)
    return sale
