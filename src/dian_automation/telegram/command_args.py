"""Separadores de argumentos de los comandos de Telegram: `|`, `;` o `,`.

Si el mensaje trae `|` se usa solo `|` (formato original; permite comas y punto y coma dentro de un valor,
por ejemplo en una razón social). Si no, separan `;` y las comas que no están entre dos dígitos, para que
un monto con comas de miles como `712,215` siga siendo un solo valor.
"""

import re
from datetime import date
from typing import List, Optional, Tuple

_ALT_SEPARATOR = re.compile(r";|(?<!\d),|,(?!\d)")

_DATE_DD_MM = re.compile(r"^(\d{1,2})/(\d{1,2})$")
_DATE_DD_MM_YYYY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_DATE_YYYY_MM_DD = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def split_args(payload: str) -> List[str]:
    """Parte el texto en argumentos ya recortados."""
    if "|" in payload:
        return [p.strip() for p in payload.split("|")]
    return [p.strip() for p in _ALT_SEPARATOR.split(payload)]


def split_first(payload: str) -> Tuple[str, Optional[str]]:
    """Separa en el primer separador: (antes, después). `después` es None si no hay separador."""
    if "|" in payload:
        head, _, tail = payload.partition("|")
        return head, tail
    match = _ALT_SEPARATOR.search(payload)
    if not match:
        return payload, None
    return payload[: match.start()], payload[match.end():]


def _parse_candidate_date(candidate: str, today: date) -> Optional[date]:
    """Valida y convierte un fragmento a date según DD/MM, DD/MM/AAAA o AAAA-MM-DD.

    Lanza ValueError si la fecha tiene formato de fecha pero no existe en el calendario (ej. 31/02).
    Devuelve None si el fragmento no tiene formato de fecha.
    """
    if not candidate:
        return None

    # Una cadena sin '/' ni '-' nunca es fecha (ej. 712,215)
    if "/" not in candidate and "-" not in candidate:
        return None

    m = _DATE_DD_MM.fullmatch(candidate)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year = today.year
        try:
            d = date(year, month, day)
            if d > today:
                d = date(year - 1, month, day)
            return d
        except ValueError:
            try:
                if (month, day) > (today.month, today.day):
                    return date(year - 1, month, day)
            except ValueError:
                pass
            raise ValueError(f"Fecha inexistente: {candidate}") from None

    m = _DATE_DD_MM_YYYY.fullmatch(candidate)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            raise ValueError(f"Fecha inexistente: {candidate}") from None

    m = _DATE_YYYY_MM_DD.fullmatch(candidate)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            raise ValueError(f"Fecha inexistente: {candidate}") from None

    return None


def split_trailing_date(
    payload: str, today: Optional[date] = None
) -> Tuple[str, Optional[date]]:
    """Separa una fecha opcional al final del payload: (resto, fecha).

    Usa los mismos separadores que `split_args` (si hay `|`, solo `|`; si no, `_ALT_SEPARATOR`).
    Si el último segmento es una fecha completa (`DD/MM`, `DD/MM/AAAA`, `AAAA-MM-DD`),
    devuelve el resto antes del último separador y la fecha convertida a `date`.
    Si no es fecha, devuelve (payload, None).
    Si parece fecha pero no existe (ej. 31/02), lanza `ValueError`.
    """
    if today is None:
        from dian_automation.core.calendar_engine import today_bogota
        today = today_bogota()

    if "|" in payload:
        head, _, tail = payload.rpartition("|")
        candidate = tail.strip()
        parsed = _parse_candidate_date(candidate, today)
        if parsed is not None:
            return head.rstrip(), parsed
        return payload, None

    matches = list(_ALT_SEPARATOR.finditer(payload))
    if matches:
        last_match = matches[-1]
        head = payload[: last_match.start()]
        tail = payload[last_match.end():]
        candidate = tail.strip()
        parsed = _parse_candidate_date(candidate, today)
        if parsed is not None:
            return head.rstrip(), parsed
        return payload, None

    candidate = payload.strip()
    if "/" in candidate or "-" in candidate:
        parsed = _parse_candidate_date(candidate, today)
        if parsed is not None:
            return "", parsed

    return payload, None
