"""Pruebas del cargador anual del calendario DIAN (Story 4.1a): validaciones, atomicidad y datos 2026."""

from collections import Counter
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.core import calendar_loader
from dian_automation.core.calendar_loader import CSV_COLUMNS, CalendarLoadError, load_calendar
from dian_automation.core.calendar_repository import find_deadlines
from dian_automation.db.models import Base, DIANTaxCalendar

ROOT = Path(__file__).resolve().parent.parent
REAL_CSV = ROOT / "data" / "calendario_dian_2026.csv"

HEADER = ",".join(CSV_COLUMNS)
DIGIT_ORDER = [1, 2, 3, 4, 5, 6, 7, 8, 9, 0]
FEB_2026 = [
    "2026-02-10", "2026-02-11", "2026-02-12", "2026-02-13", "2026-02-16",
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
]


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


@pytest.fixture
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


def _line(tax, fy, label, key_length, key_from, key_to, deadline, installment="", jurisdiction="",
          period_start="", period_end="", description=""):
    return ",".join(str(v) for v in (
        tax, fy, label, period_start, period_end, key_length, key_from, key_to, installment,
        deadline, jurisdiction, description,
    ))


def _digit_group(tax="TEST_UNO", fy=2026, label="Enero", dates=FEB_2026, **kwargs):
    return [_line(tax, fy, label, 1, d, d, when, **kwargs) for d, when in zip(DIGIT_ORDER, dates)]


def _csv(*lines, header=HEADER):
    flat = []
    for item in lines:
        flat.extend(item if isinstance(item, list) else [item])
    return header + "\n" + "\n".join(flat) + "\n"


def _write(tmp_path, text, name="calendario.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _reject(db, tmp_path, text):
    with pytest.raises(CalendarLoadError) as exc:
        load_calendar(db, _write(tmp_path, text))
    return exc.value.errors


def _count(db, **filters) -> int:
    return db.query(DIANTaxCalendar).filter_by(**filters).count()


def _all_errors(errors) -> str:
    return "\n".join(errors)


# ---------- datos reales 2026 ----------


def test_real_2026_csv_loads_with_expected_counts(db):
    counts = load_calendar(db, REAL_CSV)

    expected = {
        "IVA_BIMESTRAL": 60,
        "IVA_CUATRIMESTRAL": 30,
        "RETEFUENTE": 120,
        "RENTA_GRANDES_CONTRIBUYENTES": 30,
        "RENTA_PERSONAS_JURIDICAS": 20,
        "RENTA_PERSONAS_NATURALES": 51,
    }
    assert counts == expected
    assert dict(Counter(r.tax_type for r in db.query(DIANTaxCalendar))) == expected
    assert _count(db) == 311


def test_real_2026_csv_reference_dates(db):
    load_calendar(db, REAL_CSV)

    def by_period(tax_type, nit):
        return {r.period_start: r.deadline_date for r in find_deadlines(db, nit, tax_type, 2026)}

    bimestral_1 = by_period("IVA_BIMESTRAL", "1")
    bimestral_0 = by_period("IVA_BIMESTRAL", "0")
    bimestral_3 = by_period("IVA_BIMESTRAL", "3")
    assert bimestral_1[date(2026, 1, 1)] == date(2026, 3, 10)
    assert bimestral_0[date(2026, 1, 1)] == date(2026, 3, 24)
    assert bimestral_3[date(2026, 5, 1)] == date(2026, 7, 14)  # el Excel decía 13
    assert by_period("IVA_CUATRIMESTRAL", "0")[date(2026, 5, 1)] == date(2026, 9, 22)
    assert by_period("RETEFUENTE", "0")[date(2026, 1, 1)] == date(2026, 2, 23)

    grandes = {r.installment: r.deadline_date for r in find_deadlines(db, "0", "RENTA_GRANDES_CONTRIBUYENTES", 2026)}
    assert grandes == {1: date(2026, 2, 23), 2: date(2026, 4, 27), 3: date(2026, 6, 24)}
    juridicas = {r.installment: r.deadline_date for r in find_deadlines(db, "1", "RENTA_PERSONAS_JURIDICAS", 2026)}
    assert juridicas == {1: date(2026, 5, 12), 2: date(2026, 7, 9)}

    naturales = {
        nit: find_deadlines(db, nit, "RENTA_PERSONAS_NATURALES", 2026)[0].deadline_date
        for nit in ("02", "66", "99", "00")
    }
    assert naturales == {
        "02": date(2026, 8, 12), "66": date(2026, 9, 28), "99": date(2026, 10, 26), "00": date(2026, 10, 26),
    }


def test_real_2026_csv_rows_carry_period_and_default_sentinels(db):
    load_calendar(db, REAL_CSV)

    row = find_deadlines(db, "1", "IVA_BIMESTRAL", 2026)[0]
    assert (row.period_start, row.period_end) == (date(2026, 1, 1), date(2026, 2, 28))
    assert (row.installment, row.jurisdiction, row.key_length) == (0, "", 1)
    assert row.nit_last_digit is None  # el bot heredado sigue leyendo solo filas antiguas hasta la Story 4.1b


# ---------- reemplazo, idempotencia y atomicidad ----------


def test_reloading_the_same_file_does_not_duplicate_rows(db):
    load_calendar(db, REAL_CSV)
    load_calendar(db, REAL_CSV)

    assert _count(db) == 311


def test_load_replaces_only_the_tax_type_and_year_pairs_present_in_the_file(db, tmp_path):
    load_calendar(db, REAL_CSV)
    other_year = _write(tmp_path, _csv(_digit_group("RETEFUENTE", 2027, "Enero", dates=[
        "2027-02-10", "2027-02-11", "2027-02-12", "2027-02-15", "2027-02-16",
        "2027-02-17", "2027-02-18", "2027-02-19", "2027-02-22", "2027-02-23",
    ])), "otro_ano.csv")
    load_calendar(db, other_year)
    assert _count(db, tax_type="RETEFUENTE", fiscal_year=2026) == 120
    assert _count(db, tax_type="RETEFUENTE", fiscal_year=2027) == 10

    corrected = FEB_2026[:-1] + ["2026-02-24"]
    load_calendar(db, _write(tmp_path, _csv(_digit_group("RETEFUENTE", 2026, "Enero", dates=corrected)), "uno.csv"))

    assert _count(db, tax_type="RETEFUENTE", fiscal_year=2026) == 10  # el par 2026 se reemplazó completo
    assert _count(db, tax_type="RETEFUENTE", fiscal_year=2027) == 10
    assert _count(db, tax_type="IVA_BIMESTRAL") == 60


def test_load_replaces_legacy_rows_of_the_same_pair_and_keeps_the_rest(db, tmp_path):
    db.add_all([
        DIANTaxCalendar(tax_type="TEST_UNO", fiscal_year=2026, period_label="Antiguo", nit_last_digit=4,
                        deadline_date=date(2026, 2, 13)),
        DIANTaxCalendar(tax_type="OTRO", fiscal_year=2026, period_label="Antiguo", nit_last_digit=4,
                        deadline_date=date(2026, 2, 13)),
    ])
    db.commit()

    load_calendar(db, _write(tmp_path, _csv(_digit_group())))

    assert _count(db, tax_type="TEST_UNO") == 10
    assert _count(db, tax_type="OTRO") == 1


def test_invalid_group_rejects_the_whole_file_and_leaves_the_database_untouched(db, tmp_path):
    load_calendar(db, REAL_CSV)
    bad_group = _digit_group("TEST_MALO", label="Feb", dates=["2026-02-14"] + FEB_2026[1:])  # sábado
    text = _csv(_digit_group("RETEFUENTE"), bad_group)

    errors = _reject(db, tmp_path, text)

    assert "fin de semana" in _all_errors(errors)
    assert _count(db, tax_type="RETEFUENTE") == 120  # ni el grupo válido reemplazó nada
    assert _count(db, tax_type="TEST_MALO") == 0
    assert _count(db) == 311


def test_database_failure_midway_rolls_back_deletions_and_insertions(db, tmp_path, monkeypatch):
    load_calendar(db, REAL_CSV)

    def boom(_rows):
        raise RuntimeError("falla simulada")

    monkeypatch.setattr(db, "add_all", boom)
    with pytest.raises(RuntimeError, match="falla simulada"):
        load_calendar(db, _write(tmp_path, _csv(_digit_group("RETEFUENTE"))))

    assert _count(db, tax_type="RETEFUENTE") == 120
    assert _count(db) == 311


# ---------- validaciones del AC #3 ----------


@pytest.mark.parametrize(
    "dates",
    [
        ["2025-12-30"] + FEB_2026[1:],  # antes del 1 de enero del año fiscal
        FEB_2026[:9] + ["2027-03-01"],  # después de febrero del año siguiente
    ],
    ids=["antes_del_anio_fiscal", "despues_de_febrero_siguiente"],
)
def test_deadline_outside_fiscal_year_window_is_rejected(db, tmp_path, dates):
    errors = _reject(db, tmp_path, _csv(_digit_group(dates=dates)))

    assert "fuera del año fiscal 2026" in _all_errors(errors)
    assert _count(db) == 0


def test_last_business_day_of_next_february_is_accepted(db, tmp_path):
    dates = ["2027-02-01", "2027-02-02", "2027-02-03", "2027-02-04", "2027-02-05",
             "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11", "2027-02-26"]

    assert load_calendar(db, _write(tmp_path, _csv(_digit_group(dates=dates)))) == {"TEST_UNO": 10}


def test_leap_year_february_29_is_accepted(db, tmp_path):
    dates = ["2028-02-14", "2028-02-15", "2028-02-16", "2028-02-17", "2028-02-18",
             "2028-02-22", "2028-02-23", "2028-02-24", "2028-02-25", "2028-02-29"]

    assert load_calendar(db, _write(tmp_path, _csv(_digit_group(fy=2027, dates=dates)))) == {"TEST_UNO": 10}


@pytest.mark.parametrize(
    "when, reason",
    [("2026-02-14", "fin de semana"), ("2026-02-15", "fin de semana")],
    ids=["sabado", "domingo"],
)
def test_weekend_deadline_is_rejected_with_its_line(db, tmp_path, when, reason):
    dates = ["2026-02-10", "2026-02-11", "2026-02-12", "2026-02-13", when,
             "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23"]

    errors = _reject(db, tmp_path, _csv(_digit_group(dates=dates)))

    assert any("línea 6" in e and reason in e for e in errors), errors  # el dígito 5 es la 5.ª fila de datos


def test_colombian_holiday_deadline_is_rejected(db, tmp_path):
    dates = ["2026-03-16", "2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20",
             "2026-03-23", "2026-03-24", "2026-03-25", "2026-03-26", "2026-03-27"]  # 23-mar: San José (observado)

    errors = _reject(db, tmp_path, _csv(_digit_group(dates=dates)))

    assert any("festivo" in e and "2026-03-23" in e for e in errors), errors


def test_deadline_that_decreases_as_the_ending_grows_is_rejected(db, tmp_path):
    dates = list(FEB_2026)
    dates[2], dates[3] = dates[3], dates[2]  # terminación 3 vence después que la 4

    errors = _reject(db, tmp_path, _csv(_digit_group(dates=dates)))

    assert "las fechas deben aumentar con la terminación" in _all_errors(errors)


def test_ending_zero_sorts_last_so_an_early_zero_is_rejected(db, tmp_path):
    dates = FEB_2026[:9] + ["2026-02-09"]  # la terminación 0 no puede vencer antes que la 9

    errors = _reject(db, tmp_path, _csv(_digit_group(dates=dates)))

    assert "las fechas deben aumentar con la terminación" in _all_errors(errors)


def test_equal_dates_for_consecutive_endings_are_allowed(db, tmp_path):
    dates = FEB_2026[:8] + ["2026-02-20", "2026-02-20"]

    assert load_calendar(db, _write(tmp_path, _csv(_digit_group(dates=dates)))) == {"TEST_UNO": 10}


def test_missing_ending_is_reported(db, tmp_path):
    lines = _digit_group()
    del lines[4]  # falta la terminación 5

    errors = _reject(db, tmp_path, _csv(lines))

    assert "faltan las terminaciones 5 a 5" in _all_errors(errors)


def test_missing_tail_of_the_domain_is_reported(db, tmp_path):
    errors = _reject(db, tmp_path, _csv(_digit_group()[:6]))  # terminaciones 1 a 6

    assert "faltan las terminaciones 7 a 9" in _all_errors(errors)
    assert "faltan las terminaciones 0 a 0" in _all_errors(errors)


def test_overlapping_ranges_are_reported(db, tmp_path):
    lines = [
        _line("TEST_UNO", 2026, "Enero", 1, 1, 5, "2026-02-10"),
        _line("TEST_UNO", 2026, "Enero", 1, 5, 9, "2026-02-13"),
        _line("TEST_UNO", 2026, "Enero", 1, 0, 0, "2026-02-16"),
    ]

    errors = _reject(db, tmp_path, _csv(lines))

    assert "se solapa" in _all_errors(errors)


def test_repeated_row_is_reported(db, tmp_path):
    lines = _digit_group() + [_digit_group()[2]]

    errors = _reject(db, tmp_path, _csv(lines))

    assert "está repetida" in _all_errors(errors)


@pytest.mark.parametrize(
    "line, fragment",
    [
        (_line("TEST_UNO", 2026, "Enero", 1, 0, 10, "2026-02-10"), "sale del dominio"),
        (_line("TEST_UNO", 2026, "Enero", 2, 0, 100, "2026-02-10"), "sale del dominio"),
        (_line("TEST_UNO", 2026, "Enero", 1, 5, 3, "2026-02-10"), "sale del dominio"),
        (_line("TEST_UNO", 2026, "Enero", 3, 0, 9, "2026-02-10"), "key_length debe ser 0, 1 o 2"),
        (_line("TEST_UNO", 2026, "Enero", 0, 1, 1, "2026-02-10"), "key_from y key_to deben ser 0"),
    ],
    ids=["un_digito_hasta_10", "dos_digitos_hasta_100", "rango_invertido", "tres_digitos", "sin_llave_con_rango"],
)
def test_key_outside_its_domain_is_rejected(db, tmp_path, line, fragment):
    errors = _reject(db, tmp_path, _csv(line))

    assert fragment in _all_errors(errors)
    assert _count(db) == 0


def test_no_key_obligation_needs_a_single_row(db, tmp_path):
    lines = [
        _line("SIN_LLAVE", 2026, "Anual", 0, 0, 0, "2026-04-15"),
        _line("SIN_LLAVE", 2026, "Anual", 0, 0, 0, "2026-04-16"),
    ]

    assert "está repetida" in _all_errors(_reject(db, tmp_path, _csv(lines)))


def test_all_problems_are_listed_not_just_the_first(db, tmp_path):
    lines = [
        _line("SIN_LLAVE", 2026, "A", 0, 0, 0, "2026-04-18"),  # sábado
        _line("SIN_LLAVE_2", 2026, "B", 0, 0, 0, "2026-04-19"),  # domingo
    ]

    errors = _reject(db, tmp_path, _csv(lines))

    assert sum("fin de semana" in e for e in errors) == 2
    assert any("línea 2" in e for e in errors) and any("línea 3" in e for e in errors)


# ---------- formato del archivo ----------


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("﻿" + _csv(_digit_group()), "BOM"),
        (_csv(_digit_group(), header=HEADER.replace(",", ";")), "Encabezado inválido"),
        (_csv(_digit_group(), header=HEADER.replace("jurisdiction", "municipio")), "Encabezado inválido"),
        (_csv(_digit_group()).replace("\n", ";\n", 1), "Encabezado inválido"),
        ("", "Encabezado inválido"),
        (HEADER + "\n", "no tiene filas de datos"),
    ],
    ids=["bom", "separador_punto_y_coma", "columna_renombrada", "columna_extra", "vacio", "solo_encabezado"],
)
def test_malformed_file_is_rejected(db, tmp_path, text, fragment):
    assert fragment in _all_errors(_reject(db, tmp_path, text))
    assert _count(db) == 0


@pytest.mark.parametrize(
    "line, fragment",
    [
        (_line("TEST_UNO", 2026, "Enero", 1, 1, 1, "10/02/2026"), "AAAA-MM-DD"),
        (_line("TEST_UNO", 2026, "Enero", 1, 1, 1, "2026-02-30"), "no es una fecha válida"),
        (_line("TEST_UNO", 2026, "Enero", 1, "uno", 1, "2026-02-10"), "key_from"),
        (_line("TEST_UNO", "dos mil", "Enero", 1, 1, 1, "2026-02-10"), "fiscal_year"),
        (_line("TEST_UNO", 2026, "", 1, 1, 1, "2026-02-10"), "period_label"),
        (_line("", 2026, "Enero", 1, 1, 1, "2026-02-10"), "tax_type"),
        (_line("TEST_UNO", 2026, "Enero", 1, 1, 1, "2026-02-10", period_start="2026-01-01"), "van juntos"),
        (_line("TEST_UNO", 2026, "Enero", 1, 1, 1, "2026-02-10", period_start="2026-02-01", period_end="2026-01-01"),
         "posterior"),
        (_line("TEST_UNO", 0, "Enero", 1, 1, 1, "2026-02-10"), "fiscal_year debe estar entre"),
        (_line("TEST_UNO", 10000, "Enero", 1, 1, 1, "2026-02-10"), "fiscal_year debe estar entre"),
        (_line("TEST_UNO", 2026, "Enero", 1, 1, 1, "2026-02-10", installment=100), "installment debe ser de 0 a 99"),
        ("TEST_UNO,2026,Enero", "se esperaban 12 columnas"),
    ],
    ids=["fecha_no_iso", "fecha_inexistente", "llave_no_numerica", "anio_no_numerico", "periodo_vacio", "impuesto_vacio",
         "periodo_a_medias", "periodo_invertido", "anio_cero", "anio_de_cinco_cifras", "cuota_enorme",
         "columnas_de_menos"],
)
def test_malformed_row_is_rejected_with_its_line(db, tmp_path, line, fragment):
    errors = _reject(db, tmp_path, _csv(line))

    assert any(e.startswith("línea 2") and fragment in e for e in errors), errors
    assert _count(db) == 0


def test_blank_installment_and_jurisdiction_are_stored_as_sentinels(db, tmp_path):
    load_calendar(db, _write(tmp_path, _csv(_digit_group(description="Formulario 300"))))

    row = db.query(DIANTaxCalendar).filter_by(key_from=1).one()
    assert (row.installment, row.jurisdiction, row.description) == (0, "", "Formulario 300")


def test_blank_lines_are_ignored(db, tmp_path):
    text = _csv(_digit_group()[:5]) + "\n" + "\n".join(_digit_group()[5:]) + "\n\n"

    assert load_calendar(db, _write(tmp_path, text)) == {"TEST_UNO": 10}


# ---------- formas distintas de obligación (AC #5) ----------


def _synthetic_csv() -> str:
    return _csv(
        _line("SIN_LLAVE", 2026, "Anual", 0, 0, 0, "2026-04-15", description="Aplica a todos los NIT"),
        _line("ICA", 2026, "Bimestre 1", 0, 0, 0, "2026-03-13", jurisdiction="Bogotá"),
        _line("ICA", 2026, "Bimestre 1", 0, 0, 0, "2026-03-20", jurisdiction="Medellín"),
        _line("MEDIOS", 2026, "Grupo 1", 2, 0, 15, "2026-04-20"),
        _line("MEDIOS", 2026, "Grupo 1", 2, 16, 40, "2026-04-22"),
        _line("MEDIOS", 2026, "Grupo 1", 2, 41, 99, "2026-04-24"),
    )


def test_synthetic_shapes_load_and_query_without_schema_changes(db, tmp_path):
    counts = load_calendar(db, _write(tmp_path, _synthetic_csv()))

    assert counts == {"SIN_LLAVE": 1, "ICA": 2, "MEDIOS": 3}
    assert [r.deadline_date for r in find_deadlines(db, "901234567", "SIN_LLAVE", 2026)] == [date(2026, 4, 15)]
    assert [r.deadline_date for r in find_deadlines(db, "901234567", "ICA", 2026, "Bogotá")] == [date(2026, 3, 13)]
    assert [r.deadline_date for r in find_deadlines(db, "901234567", "ICA", 2026, "Medellín")] == [date(2026, 3, 20)]
    assert find_deadlines(db, "901234567", "ICA", 2026) == []  # el calendario nacional no incluye municipios
    by_nit = {nit: find_deadlines(db, nit, "MEDIOS", 2026)[0].deadline_date for nit in ("900000007", "900000016", "900000099", "900000000")}
    assert by_nit == {
        "900000007": date(2026, 4, 20), "900000016": date(2026, 4, 22),
        "900000099": date(2026, 4, 24), "900000000": date(2026, 4, 20),
    }


def test_range_starting_at_zero_sorts_first_so_it_cannot_have_the_latest_date(db, tmp_path):
    text = _csv(
        _line("MEDIOS", 2026, "Grupo 1", 2, 0, 15, "2026-04-24"),
        _line("MEDIOS", 2026, "Grupo 1", 2, 16, 40, "2026-04-22"),
        _line("MEDIOS", 2026, "Grupo 1", 2, 41, 99, "2026-04-20"),
    )

    assert "las fechas deben aumentar con la terminación" in _all_errors(_reject(db, tmp_path, text))


def test_same_obligation_in_different_jurisdictions_can_have_different_key_shapes(db, tmp_path):
    text = _csv(
        _line("ICA", 2026, "Bimestre 1", 0, 0, 0, "2026-03-13", jurisdiction="Bogotá"),
        [_line("ICA", 2026, "Bimestre 1", 1, d, d, when, jurisdiction="Cali") for d, when in zip(DIGIT_ORDER, FEB_2026)],
    )

    assert load_calendar(db, _write(tmp_path, text)) == {"ICA": 11}


# ---------- comando ----------


def test_cli_loads_the_file_and_reports_counts(session_factory, tmp_path, monkeypatch, capsys):
    from dian_automation.db import database

    monkeypatch.setattr(database, "SessionLocal", session_factory)

    assert calendar_loader.main([str(REAL_CSV)]) == 0

    out = capsys.readouterr().out
    assert "311 filas" in out and "RETEFUENTE: 120" in out
    check = session_factory()
    try:
        assert _count(check) == 311
    finally:
        check.close()


def test_cli_on_an_unmigrated_database_points_to_alembic(tmp_path, monkeypatch, capsys):
    from dian_automation.db import database

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})  # sin tablas
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))

    assert calendar_loader.main([str(REAL_CSV)]) == 1

    assert "alembic upgrade head" in capsys.readouterr().err
    engine.dispose()


def test_cli_rejects_a_bad_file_with_exit_code_1_and_lists_errors(session_factory, tmp_path, monkeypatch, capsys):
    from dian_automation.db import database

    monkeypatch.setattr(database, "SessionLocal", session_factory)
    bad = _write(tmp_path, _csv(_line("SIN_LLAVE", 2026, "A", 0, 0, 0, "2026-04-18")))  # sábado

    assert calendar_loader.main([str(bad)]) == 1

    err = capsys.readouterr().err
    assert "no se modificó la base" in err and "línea 2" in err and "fin de semana" in err
