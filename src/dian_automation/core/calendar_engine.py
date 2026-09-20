"""Motor de cálculo de obligaciones del calendario tributario DIAN (Story 4.1b)."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from dian_automation.config import config
from dian_automation.core.calendar_repository import find_deadlines
from dian_automation.db.models import (
    Business,
    DIANTaxCalendar,
    INCOME_SOURCE_MANUAL_SALES,
    IVA_PERIODICITY_BIMESTRAL,
    IVA_PERIODICITY_CUATRIMESTRAL,
)

_BOGOTA_TZ = ZoneInfo("America/Bogota")

_MONTH_ABBR = (
    "ene",
    "feb",
    "mar",
    "abr",
    "may",
    "jun",
    "jul",
    "ago",
    "sep",
    "oct",
    "nov",
    "dic",
)


class CalendarNotLoadedError(Exception):
    """El calendario tributario del año fiscal consultado no está cargado."""


@dataclass(frozen=True)
class Obligation:
    """Representa una obligación tributaria con fecha límite calculada."""

    tax_type: str  # IVA_BIMESTRAL | IVA_CUATRIMESTRAL | RETEFUENTE | RENTA_PERSONAS_NATURALES | RENTA_PERSONAS_JURIDICAS
    etiqueta: str  # period_label; si installment > 0 se le agrega " · Cuota N"
    fecha_limite: date
    estado: str  # "completado" | "proximo" | "aldia"
    dias: Optional[int]  # días que faltan (>= 0) para proximo y aldia; None si completado
    period_start: Optional[date]
    period_end: Optional[date]
    installment: int
    description: Optional[str]


def today_bogota() -> date:
    """Fecha de hoy en la zona horaria America/Bogota."""
    return datetime.now(_BOGOTA_TZ).date()


def format_limit_date(d: date) -> str:
    """Formatea la fecha como '14 sep 2026' (día sin cero a la izquierda, mes en minúscula de tres letras, año)."""
    return f"{d.day} {_MONTH_ABBR[d.month - 1]} {d.year}"


def iva_obligation_for_month(
    obligations: Sequence[Obligation], period_ym: str
) -> Optional[Obligation]:
    """Devuelve la obligación cuyo tax_type empieza por 'IVA_' y cuyo periodo cubre ese mes ('YYYY-MM').

    period_start <= date(año, mes, 1) <= period_end.
    None si no hay coincidencia o si period_ym es inválido.
    """
    if not isinstance(period_ym, str):
        return None

    parts = period_ym.strip().split("-")
    if len(parts) != 2:
        return None

    try:
        year = int(parts[0])
        month = int(parts[1])
        target_date = date(year, month, 1)
    except (ValueError, TypeError, OverflowError):
        return None

    for ob in obligations:
        if (
            ob.tax_type.startswith("IVA_")
            and ob.period_start is not None
            and ob.period_end is not None
            and ob.period_start <= target_date <= ob.period_end
        ):
            return ob

    return None


def next_pending(obligations: Sequence[Obligation]) -> Optional[Obligation]:
    """Primera obligación (la lista ya viene ordenada por fecha) cuyo estado no es 'completado'; None si no hay."""
    for ob in obligations:
        if ob.estado != "completado":
            return ob
    return None


class TaxCalendarEngine:
    """Motor que calcula las obligaciones tributarias aplicables a un negocio."""

    def __init__(self, db: Session, upcoming_days: Optional[int] = None):
        self.db = db
        self.upcoming_days = (
            upcoming_days if upcoming_days is not None else config.calendar_upcoming_days
        )

    def obligations(self, business: Business, today: Optional[date] = None) -> List[Obligation]:
        """Calcula y retorna la lista de obligaciones tributarias ordenadas para el negocio."""
        if business.income_source == INCOME_SOURCE_MANUAL_SALES:
            return []

        if today is None:
            today = today_bogota()

        has_calendar = (
            self.db.query(DIANTaxCalendar.id)
            .filter(DIANTaxCalendar.fiscal_year == today.year)
            .first()
            is not None
        )
        if not has_calendar:
            raise CalendarNotLoadedError(
                f"El calendario tributario del año {today.year} no está cargado."
            )

        tax_types: List[str] = []
        if business.iva_periodicity == IVA_PERIODICITY_BIMESTRAL:
            tax_types.append("IVA_BIMESTRAL")
        elif business.iva_periodicity == IVA_PERIODICITY_CUATRIMESTRAL:
            tax_types.append("IVA_CUATRIMESTRAL")

        if business.is_withholding_agent:
            tax_types.append("RETEFUENTE")

        if business.taxpayer_type == "PERSONA_NATURAL":
            tax_types.append("RENTA_PERSONAS_NATURALES")
        elif business.taxpayer_type == "PERSONA_JURIDICA":
            tax_types.append("RENTA_PERSONAS_JURIDICAS")

        rows: List[DIANTaxCalendar] = []
        for tax_type in tax_types:
            # Obligaciones del año anterior que vencen hoy o después (cruzan el año)
            prev_rows = find_deadlines(self.db, business.nit, tax_type, today.year - 1)
            for r in prev_rows:
                if r.deadline_date >= today:
                    rows.append(r)

            # Obligaciones del año fiscal en curso
            curr_rows = find_deadlines(self.db, business.nit, tax_type, today.year)
            rows.extend(curr_rows)

        result: List[Obligation] = []
        for r in rows:
            installment_val = int(r.installment) if r.installment is not None else 0
            if installment_val > 0:
                etiqueta = f"{r.period_label} · Cuota {installment_val}"
            else:
                etiqueta = r.period_label

            diff = (r.deadline_date - today).days
            if diff < 0:
                estado = "completado"
                dias = None
            elif diff <= self.upcoming_days:
                estado = "proximo"
                dias = diff
            else:
                estado = "aldia"
                dias = diff

            result.append(
                Obligation(
                    tax_type=r.tax_type,
                    etiqueta=etiqueta,
                    fecha_limite=r.deadline_date,
                    estado=estado,
                    dias=dias,
                    period_start=r.period_start,
                    period_end=r.period_end,
                    installment=installment_val,
                    description=r.description,
                )
            )

        result.sort(key=lambda o: (o.fecha_limite, o.tax_type, o.installment))
        return result
