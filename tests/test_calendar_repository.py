"""Pruebas de la consulta del calendario DIAN por terminación de NIT (Story 4.1a)."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.core.calendar_loader import load_calendar
from dian_automation.core.calendar_repository import find_deadlines
from dian_automation.db.models import Base, DIANTaxCalendar

REAL_CSV = Path(__file__).resolve().parent.parent / "data" / "calendario_dian_2026.csv"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    load_calendar(session, REAL_CSV)
    yield session
    session.close()
    engine.dispose()


def _dates(rows):
    return [r.deadline_date for r in rows]


def test_one_digit_nit_returns_one_row_per_period_ordered_by_deadline(db):
    rows = find_deadlines(db, "901008579", "IVA_BIMESTRAL", 2026)  # termina en 9

    assert _dates(rows) == [
        date(2026, 3, 20), date(2026, 5, 25), date(2026, 7, 23),
        date(2026, 9, 21), date(2026, 11, 24), date(2027, 1, 25),
    ]


def test_nit_ending_in_zero_uses_the_zero_ending(db):
    rows = find_deadlines(db, "800100200", "IVA_BIMESTRAL", 2026)

    assert _dates(rows) == [
        date(2026, 3, 24), date(2026, 5, 26), date(2026, 7, 24),
        date(2026, 9, 22), date(2026, 11, 25), date(2027, 1, 26),
    ]


@pytest.mark.parametrize(
    "nit, expected",
    [
        ("12345000", date(2026, 10, 26)),  # 00
        ("12345099", date(2026, 10, 26)),  # 99
        ("12345002", date(2026, 8, 12)),  # 01-02
        ("12345001", date(2026, 8, 12)),  # 01-02
        ("12345065", date(2026, 9, 28)),  # 65-66
        ("12345066", date(2026, 9, 28)),
        ("12345067", date(2026, 10, 1)),  # 67-68
        ("12345098", date(2026, 10, 23)),  # 97-98
    ],
)
def test_two_digit_endings_including_00_and_99(db, nit, expected):
    rows = find_deadlines(db, nit, "RENTA_PERSONAS_NATURALES", 2026)

    assert _dates(rows) == [expected]


def test_short_and_zero_padded_nits_use_two_digit_keys_with_leading_zero(db):
    assert _dates(find_deadlines(db, "7", "RENTA_PERSONAS_NATURALES", 2026)) == [date(2026, 8, 18)]  # 07-08
    assert _dates(find_deadlines(db, "007", "RENTA_PERSONAS_NATURALES", 2026)) == [date(2026, 8, 18)]
    assert _dates(find_deadlines(db, "0", "RENTA_PERSONAS_NATURALES", 2026)) == [date(2026, 10, 26)]  # 00


def test_nit_with_separators_counts_only_its_digits(db):
    assert _dates(find_deadlines(db, "901.008.579", "IVA_CUATRIMESTRAL", 2026)) == [
        date(2026, 5, 25), date(2026, 9, 21), date(2027, 1, 25),
    ]


def test_installments_of_one_obligation_come_ordered_by_date(db):
    rows = find_deadlines(db, "5", "RENTA_GRANDES_CONTRIBUYENTES", 2026)

    assert [(r.installment, r.deadline_date) for r in rows] == [
        (1, date(2026, 2, 16)), (2, date(2026, 4, 20)), (3, date(2026, 6, 17)),
    ]


def test_unknown_tax_type_or_year_returns_nothing(db):
    assert find_deadlines(db, "1", "PATRIMONIO", 2026) == []
    assert find_deadlines(db, "1", "IVA_BIMESTRAL", 2025) == []


def test_nit_without_digits_is_rejected(db):
    with pytest.raises(ValueError, match="dígitos"):
        find_deadlines(db, "", "IVA_BIMESTRAL", 2026)
    with pytest.raises(ValueError):
        find_deadlines(db, "-", "IVA_BIMESTRAL", 2026)


def test_legacy_rows_created_with_only_nit_last_digit_are_found(db):
    """Filas del esquema anterior (seed, verify_story_2_3): la llave sale de `nit_last_digit`."""
    db.add_all([
        DIANTaxCalendar(tax_type="IVA BIMESTRAL", fiscal_year=2026, period_label="Jul – Ago 2026",
                        nit_last_digit=9, deadline_date=date(2026, 9, 21)),
        DIANTaxCalendar(tax_type="IVA BIMESTRAL", fiscal_year=2026, period_label="Jul – Ago 2026",
                        nit_last_digit=0, deadline_date=date(2026, 9, 22)),
    ])
    db.commit()

    assert _dates(find_deadlines(db, "901008579", "IVA BIMESTRAL", 2026)) == [date(2026, 9, 21)]
    assert _dates(find_deadlines(db, "800100200", "IVA BIMESTRAL", 2026)) == [date(2026, 9, 22)]
    legacy = db.query(DIANTaxCalendar).filter_by(tax_type="IVA BIMESTRAL", nit_last_digit=0).one()
    assert (legacy.key_length, legacy.key_from, legacy.key_to, legacy.installment, legacy.jurisdiction) == (
        1, 0, 0, 0, "",
    )
