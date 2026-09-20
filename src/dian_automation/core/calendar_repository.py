"""Consulta del calendario tributario DIAN por terminación de NIT (Story 4.1a).

Solo lee `dian_tax_calendar`; qué obligaciones se muestran a cada negocio lo decide el motor de la Story 4.1b.
"""

from typing import List

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from dian_automation.db.models import DIANTaxCalendar


def find_deadlines(
    db: Session, nit: str, tax_type: str, fiscal_year: int, jurisdiction: str = ""
) -> List[DIANTaxCalendar]:
    """Filas de `tax_type` y `fiscal_year` cuya llave cubre la terminación del NIT, por fecha límite.

    `nit` es el NIT sin dígito de verificación; solo cuentan sus dígitos. Una llave de un dígito compara
    el último dígito; una de dos dígitos, los dos últimos (`7` equivale a `07`). Las filas sin llave
    (`key_length = 0`) siempre aplican. `jurisdiction` vacío es el calendario nacional.
    """
    digits = "".join(ch for ch in str(nit) if ch.isdigit())
    if not digits:
        raise ValueError("El NIT no tiene dígitos")
    last_one = int(digits[-1])
    last_two = int(digits[-2:])

    return (
        db.query(DIANTaxCalendar)
        .filter(
            DIANTaxCalendar.tax_type == tax_type,
            DIANTaxCalendar.fiscal_year == fiscal_year,
            DIANTaxCalendar.jurisdiction == jurisdiction,
            or_(
                DIANTaxCalendar.key_length == 0,
                and_(
                    DIANTaxCalendar.key_length == 1,
                    DIANTaxCalendar.key_from <= last_one,
                    DIANTaxCalendar.key_to >= last_one,
                ),
                and_(
                    DIANTaxCalendar.key_length == 2,
                    DIANTaxCalendar.key_from <= last_two,
                    DIANTaxCalendar.key_to >= last_two,
                ),
            ),
        )
        .order_by(DIANTaxCalendar.deadline_date, DIANTaxCalendar.installment, DIANTaxCalendar.id)
        .all()
    )
