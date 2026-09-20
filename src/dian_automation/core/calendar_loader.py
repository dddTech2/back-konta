"""Carga anual del calendario tributario DIAN (Story 4.1a).

Lee un CSV con las columnas de `CSV_COLUMNS`, lo valida completo y reemplaza, en una sola transacción, las
filas de cada par `(tax_type, fiscal_year)` presente en el archivo. Si cualquier regla falla se rechaza el
archivo entero (`CalendarLoadError` lista cada problema por línea) y la base queda intacta.

Uso: `uv run python -m dian_automation.core.calendar_loader data/calendario_dian_2026.csv`
"""

import argparse
import calendar
import csv
import io
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import holidays
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from dian_automation.db.models import DIANTaxCalendar

CSV_COLUMNS = (
    "tax_type",
    "fiscal_year",
    "period_label",
    "period_start",
    "period_end",
    "key_length",
    "key_from",
    "key_to",
    "installment",
    "deadline_date",
    "jurisdiction",
    "description",
)

# Cantidad de terminaciones posibles según los dígitos de la llave (0 = sin llave: una sola "terminación").
_DOMAIN_SIZE = {0: 1, 1: 10, 2: 100}

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MAX_TAX_TYPE = 50
_MAX_PERIOD_LABEL = 100
_MAX_JURISDICTION = 60
_MIN_FISCAL_YEAR, _MAX_FISCAL_YEAR = 2000, 2100
_MAX_INSTALLMENT = 99


class CalendarLoadError(Exception):
    """El archivo no se cargó; `errors` trae un mensaje por cada problema encontrado."""

    def __init__(self, errors: List[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class CalendarRow:
    line: int
    tax_type: str
    fiscal_year: int
    period_label: str
    period_start: Optional[date]
    period_end: Optional[date]
    key_length: int
    key_from: int
    key_to: int
    installment: int
    deadline_date: date
    jurisdiction: str
    description: Optional[str]

    @property
    def group(self) -> Tuple[str, int, str, int, str]:
        """Conjunto de filas que deben cubrir todas las terminaciones y crecer con ellas."""
        return (self.tax_type, self.fiscal_year, self.period_label, self.installment, self.jurisdiction)

    @property
    def order_rank(self) -> int:
        """Posición de la fila en el orden de terminaciones 1,2,…,9,0 (01,…,99,00 con dos dígitos).

        Un rango de una sola terminación `0` va al final; un rango que empieza en 0 y sigue (`00-15`) al inicio.
        """
        if self.key_length and self.key_from == 0 and self.key_to == 0:
            return _DOMAIN_SIZE[self.key_length]
        return self.key_from


def _describe(row: CalendarRow) -> str:
    width = row.key_length
    if width == 0:
        return "sin llave de NIT"
    lo, hi = str(row.key_from).zfill(width), str(row.key_to).zfill(width)
    return lo if lo == hi else f"{lo}-{hi}"


def _group_label(group: Tuple[str, int, str, int, str]) -> str:
    tax_type, fiscal_year, period_label, installment, jurisdiction = group
    extra = (f", cuota {installment}" if installment else "") + (f", {jurisdiction}" if jurisdiction else "")
    return f"{tax_type} {fiscal_year} «{period_label}»{extra}"


def _parse_int(raw: str, field: str) -> int:
    if not re.fullmatch(r"\d{1,6}", raw):
        raise ValueError(f"{field} debe ser un entero no negativo (llegó «{raw}»)")
    return int(raw)


def _parse_date(raw: str, field: str) -> date:
    if not _ISO_DATE.fullmatch(raw):
        raise ValueError(f"{field} debe tener formato AAAA-MM-DD (llegó «{raw}»)")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{field} no es una fecha válida (llegó «{raw}»)") from None


def _parse_row(line: int, cells: List[str]) -> CalendarRow:
    values = dict(zip(CSV_COLUMNS, (c.strip() for c in cells)))

    tax_type = values["tax_type"]
    if not tax_type or len(tax_type) > _MAX_TAX_TYPE:
        raise ValueError(f"tax_type es obligatorio y admite hasta {_MAX_TAX_TYPE} caracteres")
    period_label = values["period_label"]
    if not period_label or len(period_label) > _MAX_PERIOD_LABEL:
        raise ValueError(f"period_label es obligatorio y admite hasta {_MAX_PERIOD_LABEL} caracteres")
    jurisdiction = values["jurisdiction"]
    if len(jurisdiction) > _MAX_JURISDICTION:
        raise ValueError(f"jurisdiction admite hasta {_MAX_JURISDICTION} caracteres")

    period_start = _parse_date(values["period_start"], "period_start") if values["period_start"] else None
    period_end = _parse_date(values["period_end"], "period_end") if values["period_end"] else None
    if (period_start is None) != (period_end is None):
        raise ValueError("period_start y period_end van juntos o ambos vacíos")
    if period_start and period_end and period_start > period_end:
        raise ValueError("period_start es posterior a period_end")

    fiscal_year = _parse_int(values["fiscal_year"], "fiscal_year")
    if not _MIN_FISCAL_YEAR <= fiscal_year <= _MAX_FISCAL_YEAR:
        raise ValueError(f"fiscal_year debe estar entre {_MIN_FISCAL_YEAR} y {_MAX_FISCAL_YEAR} (llegó {fiscal_year})")
    installment = _parse_int(values["installment"], "installment") if values["installment"] else 0
    if installment > _MAX_INSTALLMENT:
        raise ValueError(f"installment debe ser de 0 a {_MAX_INSTALLMENT} (llegó {installment})")

    return CalendarRow(
        line=line,
        tax_type=tax_type,
        fiscal_year=fiscal_year,
        period_label=period_label,
        period_start=period_start,
        period_end=period_end,
        key_length=_parse_int(values["key_length"], "key_length"),
        key_from=_parse_int(values["key_from"], "key_from"),
        key_to=_parse_int(values["key_to"], "key_to"),
        installment=installment,
        deadline_date=_parse_date(values["deadline_date"], "deadline_date"),
        jurisdiction=jurisdiction,
        description=values["description"] or None,
    )


def read_csv(path: Path) -> List[CalendarRow]:
    """Lee el CSV con encabezado exacto y separador coma. Cualquier fila mal formada falla el archivo."""
    with open(path, encoding="utf-8", newline="") as handle:
        text = handle.read()
    if text.startswith("\ufeff"):
        raise CalendarLoadError(["El archivo tiene BOM: guárdelo como UTF-8 sin BOM."])

    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None or tuple(h.strip() for h in header) != CSV_COLUMNS:
        raise CalendarLoadError(
            ["Encabezado inválido: se esperaba «" + ",".join(CSV_COLUMNS) + "» separado por comas."]
        )

    rows: List[CalendarRow] = []
    errors: List[str] = []
    for cells in reader:
        if not cells:  # línea en blanco
            continue
        line = reader.line_num
        if len(cells) != len(CSV_COLUMNS):
            errors.append(f"línea {line}: se esperaban {len(CSV_COLUMNS)} columnas y hay {len(cells)}")
            continue
        try:
            rows.append(_parse_row(line, cells))
        except ValueError as exc:
            errors.append(f"línea {line}: {exc}")
    if errors:
        raise CalendarLoadError(errors)
    if not rows:
        raise CalendarLoadError(["El archivo no tiene filas de datos."])
    return rows


def _row_errors(row: CalendarRow, holiday_cache: Dict[int, "holidays.HolidayBase"]) -> List[str]:
    """Reglas de una fila por sí sola: dominio de la llave, año fiscal, fin de semana y festivos."""
    errors: List[str] = []
    line = f"línea {row.line}"

    if row.key_length not in _DOMAIN_SIZE:
        errors.append(f"{line}: key_length debe ser 0, 1 o 2 (llegó {row.key_length})")
    elif row.key_length == 0:
        if (row.key_from, row.key_to) != (0, 0):
            errors.append(f"{line}: sin llave de NIT (key_length 0) key_from y key_to deben ser 0")
    elif not 0 <= row.key_from <= row.key_to < _DOMAIN_SIZE[row.key_length]:
        errors.append(
            f"{line}: el rango {row.key_from}-{row.key_to} sale del dominio de {row.key_length} "
            f"dígito(s) (0 a {_DOMAIN_SIZE[row.key_length] - 1}) o está invertido"
        )

    first_day = date(row.fiscal_year, 1, 1)
    last_day = date(row.fiscal_year + 1, 2, calendar.monthrange(row.fiscal_year + 1, 2)[1])
    if not first_day <= row.deadline_date <= last_day:
        errors.append(
            f"{line}: la fecha {row.deadline_date} está fuera del año fiscal {row.fiscal_year} "
            f"({first_day} a {last_day})"
        )

    if row.deadline_date.weekday() >= 5:
        errors.append(f"{line}: la fecha {row.deadline_date} cae en fin de semana")
    else:
        year = row.deadline_date.year
        if year not in holiday_cache:
            holiday_cache[year] = holidays.Colombia(years=year)
        festivo = holiday_cache[year].get(row.deadline_date)
        if festivo:
            errors.append(f"{line}: la fecha {row.deadline_date} es festivo en Colombia ({festivo})")
    return errors


def _group_errors(group, rows: List[CalendarRow]) -> List[str]:
    """Reglas de un grupo: una sola longitud de llave, sin filas repetidas ni solapes, cobertura y orden."""
    label = _group_label(group)
    lengths = {r.key_length for r in rows}
    if len(lengths) > 1:
        return [f"{label}: mezcla llaves de {sorted(lengths)} dígitos (línea {rows[0].line} y siguientes)"]
    key_length = lengths.pop()
    if key_length not in _DOMAIN_SIZE:
        return []  # ya reportado por la fila

    errors: List[str] = []
    repeated = Counter((r.key_from, r.key_to) for r in rows)
    seen = set()
    for r in rows:
        key = (r.key_from, r.key_to)
        if repeated[key] > 1 and key in seen:
            errors.append(f"línea {r.line}: {label}: la terminación {_describe(r)} está repetida")
        seen.add(key)
    if errors:
        return errors

    domain = _DOMAIN_SIZE[key_length]
    in_domain = [r for r in rows if 0 <= r.key_from <= r.key_to < domain]  # el resto ya se reportó por fila
    next_expected = 0
    for r in sorted(in_domain, key=lambda r: (r.key_from, r.key_to)):
        if r.key_from > next_expected:
            errors.append(
                f"{label}: faltan las terminaciones {next_expected} a {r.key_from - 1} (antes de la línea {r.line})"
            )
        elif r.key_from < next_expected:
            errors.append(f"línea {r.line}: {label}: la terminación {_describe(r)} se solapa con otra fila")
        next_expected = max(next_expected, r.key_to + 1)
    if next_expected < domain:
        errors.append(f"{label}: faltan las terminaciones {next_expected} a {domain - 1}")

    ordered = sorted(in_domain, key=lambda r: (r.order_rank, r.key_to))
    for previous, current in zip(ordered, ordered[1:]):
        if current.deadline_date < previous.deadline_date:
            errors.append(
                f"línea {current.line}: {label}: la terminación {_describe(current)} vence el "
                f"{current.deadline_date}, antes que la {_describe(previous)} ({previous.deadline_date}); "
                "las fechas deben aumentar con la terminación"
            )
    return errors


def validate_rows(rows: Iterable[CalendarRow]) -> List[str]:
    """Devuelve todos los problemas del conjunto de filas (lista vacía si el archivo es válido)."""
    rows = list(rows)
    errors: List[str] = []
    holiday_cache: Dict[int, "holidays.HolidayBase"] = {}
    for row in rows:
        errors.extend(_row_errors(row, holiday_cache))

    groups: Dict[tuple, List[CalendarRow]] = defaultdict(list)
    for row in rows:
        groups[row.group].append(row)
    for group, group_rows in groups.items():
        errors.extend(_group_errors(group, group_rows))
    return errors


def load_rows(db: Session, rows: List[CalendarRow]) -> Dict[str, int]:
    """Reemplaza las filas de cada `(tax_type, fiscal_year)` presente; devuelve las filas cargadas por `tax_type`."""
    pairs = sorted({(r.tax_type, r.fiscal_year) for r in rows})
    try:
        for tax_type, fiscal_year in pairs:
            db.query(DIANTaxCalendar).filter(
                DIANTaxCalendar.tax_type == tax_type, DIANTaxCalendar.fiscal_year == fiscal_year
            ).delete(synchronize_session=False)
        db.add_all(
            DIANTaxCalendar(
                tax_type=r.tax_type,
                fiscal_year=r.fiscal_year,
                period_label=r.period_label,
                period_start=r.period_start,
                period_end=r.period_end,
                key_length=r.key_length,
                key_from=r.key_from,
                key_to=r.key_to,
                installment=r.installment,
                jurisdiction=r.jurisdiction,
                nit_last_digit=None,
                deadline_date=r.deadline_date,
                description=r.description,
            )
            for r in rows
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return dict(Counter(r.tax_type for r in rows))


def load_calendar(db: Session, path: Path) -> Dict[str, int]:
    """Lee, valida y carga el CSV; lanza `CalendarLoadError` sin tocar la base si algo no cumple."""
    rows = read_csv(Path(path))
    errors = validate_rows(rows)
    if errors:
        raise CalendarLoadError(errors)
    return load_rows(db, rows)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Carga el calendario tributario DIAN de un año desde un CSV.")
    parser.add_argument("csv_path", type=Path, help="CSV con el calendario (encabezado exacto, separado por comas)")
    args = parser.parse_args(argv)

    from dian_automation.db.database import SessionLocal  # importa la URL de base de datos al usarlo

    db = SessionLocal()
    try:
        counts = load_calendar(db, args.csv_path)
    except CalendarLoadError as exc:
        print(f"Carga rechazada ({len(exc.errors)} problema(s)); no se modificó la base:", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    except OperationalError as exc:
        print(
            f"Error de base de datos: {exc.orig}. ¿Aplicó `uv run alembic upgrade head` antes de cargar?",
            file=sys.stderr,
        )
        return 1
    finally:
        db.close()

    print(f"Calendario cargado: {sum(counts.values())} filas.")
    for tax_type, total in sorted(counts.items()):
        print(f"  {tax_type}: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
