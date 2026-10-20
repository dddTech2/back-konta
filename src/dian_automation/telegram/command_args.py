"""Separadores de argumentos de los comandos de Telegram: `|`, `;` o `,`.

Si el mensaje trae `|` se usa solo `|` (formato original; permite comas y punto y coma dentro de un valor,
por ejemplo en una razón social). Si no, separan `;` y las comas que no están entre dos dígitos, para que
un monto con comas de miles como `712,215` siga siendo un solo valor.
"""

import re
from typing import List, Optional, Tuple

_ALT_SEPARATOR = re.compile(r";|(?<!\d),|,(?!\d)")


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
