"""Utilidades para rangos y filtrado de periodos en columnas DateTime."""

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy import and_, false

_YEAR_RE = re.compile(r"^\d{4}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def period_range(prefix: str) -> Optional[Tuple[datetime, datetime]]:
    """Devuelve (inicio_inclusivo, fin_exclusivo) para prefijos AAAA, AAAA-MM o AAAA-MM-DD.

    Retorna None si el prefijo es vacío, inválido o representa una fecha inexistente.
    Ignora espacios al inicio y al final.
    """
    if not prefix:
        return None
    cleaned = prefix.strip()
    if not cleaned:
        return None

    if _YEAR_RE.match(cleaned):
        try:
            year = int(cleaned)
            if not (1 <= year <= 9999):
                return None
            start = datetime(year, 1, 1)
            end = datetime(year + 1, 1, 1)
            return (start, end)
        except (ValueError, OverflowError):
            return None

    if _MONTH_RE.match(cleaned):
        try:
            parts = cleaned.split("-")
            year, month = int(parts[0]), int(parts[1])
            start = datetime(year, month, 1)
            if month == 12:
                end = datetime(year + 1, 1, 1)
            else:
                end = datetime(year, month + 1, 1)
            return (start, end)
        except (ValueError, OverflowError):
            return None

    if _DAY_RE.match(cleaned):
        try:
            parts = cleaned.split("-")
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            start = datetime(year, month, day)
            end = start + timedelta(days=1)
            return (start, end)
        except (ValueError, OverflowError):
            return None

    return None


def period_clause(column, prefix: str):
    """Devuelve una cláusula and_(col >= inicio, col < fin) o false() si el rango es inválido."""
    rng = period_range(prefix)
    if rng is None:
        return false()
    start, end = rng
    return and_(column >= start, column < end)
