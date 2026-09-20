"""Pruebas unitarias para el motor de calendario tributario (Story 4.1b - AC #1 a #3)."""

from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.core.calendar_engine import (
    CalendarNotLoadedError,
    Obligation,
    TaxCalendarEngine,
    format_limit_date,
    iva_obligation_for_month,
    next_pending,
    today_bogota,
)
from dian_automation.db.models import (
    Base,
    Business,
    DIANTaxCalendar,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    IVA_PERIODICITY_BIMESTRAL,
    IVA_PERIODICITY_CUATRIMESTRAL,
)


@pytest.fixture
def db():
    """Sesión de base de datos SQLite en memoria para pruebas."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _seed_calendar_2026(db_session):
    """Siembra manual de un calendario 2026 mínimo."""
    rows = [
        # IVA BIMESTRAL (Periodo 1: termina en 1..9 vence 20 mar, termina en 0 vence 24 mar)
        DIANTaxCalendar(
            tax_type="IVA_BIMESTRAL",
            fiscal_year=2026,
            period_label="Ene – Feb 2026",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 2, 28),
            key_length=1,
            key_from=1,
            key_to=9,
            installment=0,
            deadline_date=date(2026, 3, 20),
            description="Declaración y pago",
        ),
        DIANTaxCalendar(
            tax_type="IVA_BIMESTRAL",
            fiscal_year=2026,
            period_label="Ene – Feb 2026",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 2, 28),
            key_length=1,
            key_from=0,
            key_to=0,
            installment=0,
            deadline_date=date(2026, 3, 24),
            description="Declaración y pago",
        ),
        DIANTaxCalendar(
            tax_type="IVA_BIMESTRAL",
            fiscal_year=2026,
            period_label="Mar – Abr 2026",
            period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 30),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=0,
            deadline_date=date(2026, 5, 20),
            description="Declaración y pago",
        ),
        # IVA BIMESTRAL periodo 6: vence en enero 2027
        DIANTaxCalendar(
            tax_type="IVA_BIMESTRAL",
            fiscal_year=2026,
            period_label="Nov – Dic 2026",
            period_start=date(2026, 11, 1),
            period_end=date(2026, 12, 31),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=0,
            deadline_date=date(2027, 1, 20),
            description="Declaración y pago",
        ),
        # IVA CUATRIMESTRAL
        DIANTaxCalendar(
            tax_type="IVA_CUATRIMESTRAL",
            fiscal_year=2026,
            period_label="Ene – Abr 2026",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 4, 30),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=0,
            deadline_date=date(2026, 5, 25),
            description="Declaración y pago",
        ),
        # RETEFUENTE
        DIANTaxCalendar(
            tax_type="RETEFUENTE",
            fiscal_year=2026,
            period_label="Ene 2026",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=0,
            deadline_date=date(2026, 2, 20),
            description="Declaración y pago",
        ),
        # RENTA PERSONAS NATURALES (llave de 2 dígitos)
        DIANTaxCalendar(
            tax_type="RENTA_PERSONAS_NATURALES",
            fiscal_year=2026,
            period_label="Renta año gravable 2025",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            key_length=2,
            key_from=1,
            key_to=2,
            installment=0,
            deadline_date=date(2026, 8, 12),
            description="Declaración y pago",
        ),
        DIANTaxCalendar(
            tax_type="RENTA_PERSONAS_NATURALES",
            fiscal_year=2026,
            period_label="Renta año gravable 2025",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            key_length=2,
            key_from=0,
            key_to=0,
            installment=0,
            deadline_date=date(2026, 10, 26),
            description="Declaración y pago",
        ),
        # RENTA PERSONAS JURIDICAS (con cuotas 1 y 2)
        DIANTaxCalendar(
            tax_type="RENTA_PERSONAS_JURIDICAS",
            fiscal_year=2026,
            period_label="Renta año gravable 2025",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=1,
            deadline_date=date(2026, 5, 12),
            description="Declaración y pago 1a cuota",
        ),
        DIANTaxCalendar(
            tax_type="RENTA_PERSONAS_JURIDICAS",
            fiscal_year=2026,
            period_label="Renta año gravable 2025",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=2,
            deadline_date=date(2026, 7, 9),
            description="Pago 2a cuota",
        ),
        # RENTA GRANDES CONTRIBUYENTES (NO debe aparecer para ningún perfil ordinario)
        DIANTaxCalendar(
            tax_type="RENTA_GRANDES_CONTRIBUYENTES",
            fiscal_year=2026,
            period_label="Renta año gravable 2025",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=1,
            deadline_date=date(2026, 2, 10),
            description="Pago 1a cuota",
        ),
    ]
    db_session.add_all(rows)
    db_session.commit()


def _make_business(
    nit: str = "901401271",
    taxpayer_type: str = "PERSONA_NATURAL",
    iva_periodicity: Optional[str] = IVA_PERIODICITY_BIMESTRAL,
    is_withholding_agent: bool = False,
    income_source: str = INCOME_SOURCE_DIAN,
) -> Business:
    """Crea una instancia de Business en memoria."""
    return Business(
        id=f"biz-{nit}",
        client_id="usr-test",
        legal_name="Empresa Test SAS",
        commercial_name="Test Comercial",
        nit=nit,
        dv="1",
        taxpayer_type=taxpayer_type,
        iva_periodicity=iva_periodicity,
        is_withholding_agent=is_withholding_agent,
        income_source=income_source,
        is_active=True,
    )


# ==============================================================================
# 1. Pruebas de funciones auxiliares
# ==============================================================================


def test_format_limit_date():
    """format_limit_date formatea día sin cero, mes de 3 letras en español y año."""
    assert format_limit_date(date(2026, 9, 14)) == "14 sep 2026"
    assert format_limit_date(date(2026, 1, 5)) == "5 ene 2026"
    assert format_limit_date(date(2026, 12, 31)) == "31 dic 2026"
    assert format_limit_date(date(2026, 4, 1)) == "1 abr 2026"

    meses = [format_limit_date(date(2026, m, 1)).split()[1] for m in range(1, 13)]
    assert meses == ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def test_today_bogota():
    """today_bogota retorna la fecha actual en America/Bogota."""
    tb = today_bogota()
    assert isinstance(tb, date)
    assert tb == datetime.now(ZoneInfo("America/Bogota")).date()


def test_iva_obligation_for_month():
    """iva_obligation_for_month encuentra la obligación de IVA que cubre el mes YYYY-MM."""
    ob_bimestral = Obligation(
        tax_type="IVA_BIMESTRAL",
        etiqueta="Ene – Feb 2026",
        fecha_limite=date(2026, 3, 20),
        estado="proximo",
        dias=10,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 28),
        installment=0,
        description="Declaración y pago",
    )
    ob_retefuente = Obligation(
        tax_type="RETEFUENTE",
        etiqueta="Ene 2026",
        fecha_limite=date(2026, 2, 20),
        estado="completado",
        dias=None,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        installment=0,
        description="Declaración y pago",
    )
    obs = [ob_bimestral, ob_retefuente]

    # Mes dentro del bimestre
    assert iva_obligation_for_month(obs, "2026-01") == ob_bimestral
    assert iva_obligation_for_month(obs, "2026-02") == ob_bimestral

    # Mes fuera del bimestre
    assert iva_obligation_for_month(obs, "2026-03") is None
    assert iva_obligation_for_month(obs, "2025-12") is None

    # Lista sin obligaciones de IVA
    assert iva_obligation_for_month([ob_retefuente], "2026-01") is None

    # Entradas inválidas
    assert iva_obligation_for_month(obs, "2026") is None
    assert iva_obligation_for_month(obs, "2026-13") is None
    assert iva_obligation_for_month(obs, "invalido") is None
    assert iva_obligation_for_month(obs, "") is None


def test_next_pending():
    """next_pending retorna la primera obligación no completada o None."""
    ob_comp = Obligation(
        tax_type="RETEFUENTE",
        etiqueta="Ene 2026",
        fecha_limite=date(2026, 2, 20),
        estado="completado",
        dias=None,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        installment=0,
        description=None,
    )
    ob_prox = Obligation(
        tax_type="IVA_BIMESTRAL",
        etiqueta="Ene – Feb 2026",
        fecha_limite=date(2026, 3, 20),
        estado="proximo",
        dias=5,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 28),
        installment=0,
        description=None,
    )
    ob_aldia = Obligation(
        tax_type="IVA_BIMESTRAL",
        etiqueta="Mar – Abr 2026",
        fecha_limite=date(2026, 5, 20),
        estado="aldia",
        dias=65,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 4, 30),
        installment=0,
        description=None,
    )

    # Con lista mixta: debe retornar el primero no completado (proximo)
    assert next_pending([ob_comp, ob_prox, ob_aldia]) == ob_prox

    # Si el primero es aldia:
    assert next_pending([ob_comp, ob_aldia]) == ob_aldia

    # Si todas están completadas:
    assert next_pending([ob_comp, ob_comp]) is None

    # Lista vacía:
    assert next_pending([]) is None


# ==============================================================================
# 2. Pruebas de TaxCalendarEngine (Perfiles, Cuotas, Estados, NITs)
# ==============================================================================


def test_profiles_return_only_relevant_tax_types(db):
    """Cada perfil tributario devuelve únicamente las obligaciones que le corresponden."""
    _seed_calendar_2026(db)
    engine = TaxCalendarEngine(db)
    fixed_today = date(2026, 1, 1)

    # Perfil 1: Persona Natural, IVA Bimestral, NO agente retenedor
    biz_pn_bim = _make_business(
        nit="901401201",  # termina en 01: la renta de personas naturales usa llave de 2 dígitos
        taxpayer_type="PERSONA_NATURAL",
        iva_periodicity=IVA_PERIODICITY_BIMESTRAL,
        is_withholding_agent=False,
    )
    obs1 = engine.obligations(biz_pn_bim, today=fixed_today)
    types1 = {o.tax_type for o in obs1}
    assert types1 == {"IVA_BIMESTRAL", "RENTA_PERSONAS_NATURALES"}
    assert "IVA_CUATRIMESTRAL" not in types1
    assert "RETEFUENTE" not in types1
    assert "RENTA_PERSONAS_JURIDICAS" not in types1
    assert "RENTA_GRANDES_CONTRIBUYENTES" not in types1

    # Perfil 2: Persona Jurídica, IVA Cuatrimestral, SÍ agente retenedor
    biz_pj_cuat = _make_business(
        taxpayer_type="PERSONA_JURIDICA",
        iva_periodicity=IVA_PERIODICITY_CUATRIMESTRAL,
        is_withholding_agent=True,
    )
    obs2 = engine.obligations(biz_pj_cuat, today=fixed_today)
    types2 = {o.tax_type for o in obs2}
    assert types2 == {"IVA_CUATRIMESTRAL", "RETEFUENTE", "RENTA_PERSONAS_JURIDICAS"}
    assert "IVA_BIMESTRAL" not in types2
    assert "RENTA_PERSONAS_NATURALES" not in types2
    assert "RENTA_GRANDES_CONTRIBUYENTES" not in types2

    # Perfil 3: Persona Natural, sin IVA (NULL), NO agente retenedor
    biz_pn_no_iva = _make_business(
        nit="901401201",
        taxpayer_type="PERSONA_NATURAL",
        iva_periodicity=None,
        is_withholding_agent=False,
    )
    obs3 = engine.obligations(biz_pn_no_iva, today=fixed_today)
    types3 = {o.tax_type for o in obs3}
    assert types3 == {"RENTA_PERSONAS_NATURALES"}
    assert "IVA_BIMESTRAL" not in types3
    assert "IVA_CUATRIMESTRAL" not in types3
    assert "RETEFUENTE" not in types3


def test_ordering_by_deadline_tax_type_and_installment(db):
    """Las obligaciones se devuelven ordenadas por (fecha_limite, tax_type, installment)."""
    _seed_calendar_2026(db)
    engine = TaxCalendarEngine(db)
    biz = _make_business(
        taxpayer_type="PERSONA_JURIDICA",
        iva_periodicity=IVA_PERIODICITY_BIMESTRAL,
        is_withholding_agent=True,
    )
    obs = engine.obligations(biz, today=date(2026, 1, 1))
    assert len(obs) >= 3

    sorted_expected = sorted(obs, key=lambda o: (o.fecha_limite, o.tax_type, o.installment))
    assert obs == sorted_expected


def test_label_with_cuotas_when_installment_greater_than_zero(db):
    """La etiqueta incluye ' · Cuota N' si installment > 0, o solo period_label si installment == 0."""
    _seed_calendar_2026(db)
    engine = TaxCalendarEngine(db)
    biz = _make_business(
        taxpayer_type="PERSONA_JURIDICA",
        iva_periodicity=IVA_PERIODICITY_BIMESTRAL,
        is_withholding_agent=False,
    )
    obs = engine.obligations(biz, today=date(2026, 1, 1))

    renta_obs = [o for o in obs if o.tax_type == "RENTA_PERSONAS_JURIDICAS"]
    assert len(renta_obs) == 2
    assert renta_obs[0].installment == 1
    assert renta_obs[0].etiqueta == "Renta año gravable 2025 · Cuota 1"
    assert renta_obs[1].installment == 2
    assert renta_obs[1].etiqueta == "Renta año gravable 2025 · Cuota 2"

    iva_obs = [o for o in obs if o.tax_type == "IVA_BIMESTRAL"]
    for o in iva_obs:
        assert o.installment == 0
        assert " · Cuota" not in o.etiqueta


def test_nit_endings_zero_and_double_zero(db):
    """Verifica resolución correcta de NITs terminados en 0 (1 dígito) y en 00 (2 dígitos)."""
    _seed_calendar_2026(db)
    engine = TaxCalendarEngine(db)
    fixed_today = date(2026, 1, 1)

    # 1. NIT terminado en 0 con llave de 1 dígito (IVA Bimestral periodo 1)
    biz_end_0 = _make_business(nit="900100200", iva_periodicity=IVA_PERIODICITY_BIMESTRAL)
    obs_0 = engine.obligations(biz_end_0, today=fixed_today)
    iva_p1_0 = next(o for o in obs_0 if o.tax_type == "IVA_BIMESTRAL" and o.etiqueta == "Ene – Feb 2026")
    assert iva_p1_0.fecha_limite == date(2026, 3, 24)  # key_from=0, key_to=0

    # NIT terminado en 1 con llave de 1 dígito
    biz_end_1 = _make_business(nit="900100201", iva_periodicity=IVA_PERIODICITY_BIMESTRAL)
    obs_1 = engine.obligations(biz_end_1, today=fixed_today)
    iva_p1_1 = next(o for o in obs_1 if o.tax_type == "IVA_BIMESTRAL" and o.etiqueta == "Ene – Feb 2026")
    assert iva_p1_1.fecha_limite == date(2026, 3, 20)  # key_from=1, key_to=9

    # 2. NIT terminado en 00 con llave de 2 dígitos (Renta Persona Natural)
    biz_end_00 = _make_business(nit="800200000", taxpayer_type="PERSONA_NATURAL", iva_periodicity=None)
    obs_00 = engine.obligations(biz_end_00, today=fixed_today)
    renta_00 = next(o for o in obs_00 if o.tax_type == "RENTA_PERSONAS_NATURALES")
    assert renta_00.fecha_limite == date(2026, 10, 26)  # key_from=0, key_to=0

    # NIT terminado en 01 con llave de 2 dígitos
    biz_end_01 = _make_business(nit="800200001", taxpayer_type="PERSONA_NATURAL", iva_periodicity=None)
    obs_01 = engine.obligations(biz_end_01, today=fixed_today)
    renta_01 = next(o for o in obs_01 if o.tax_type == "RENTA_PERSONAS_NATURALES")
    assert renta_01.fecha_limite == date(2026, 8, 12)  # key_from=1, key_to=2


def test_three_states_and_upcoming_days_logic(db):
    """Verifica los tres estados ('completado', 'proximo', 'aldia') con upcoming_days por defecto y custom."""
    _seed_calendar_2026(db)
    # Fecha límite de referencia: IVA Mar - Abr 2026 vence el 2026-05-20
    biz = _make_business(iva_periodicity=IVA_PERIODICITY_BIMESTRAL, taxpayer_type="OTRO")

    # A. default upcoming_days = 15
    engine_default = TaxCalendarEngine(db, upcoming_days=15)

    # 1. Hoy es después de la fecha límite -> 'completado', dias=None
    obs_comp = engine_default.obligations(biz, today=date(2026, 5, 21))
    target_comp = next(o for o in obs_comp if o.fecha_limite == date(2026, 5, 20))
    assert target_comp.estado == "completado"
    assert target_comp.dias is None

    # 2. Hoy es exactamente la fecha límite -> 'proximo', dias=0
    obs_exact = engine_default.obligations(biz, today=date(2026, 5, 20))
    target_exact = next(o for o in obs_exact if o.fecha_limite == date(2026, 5, 20))
    assert target_exact.estado == "proximo"
    assert target_exact.dias == 0

    # 3. Faltan 15 días (en el umbral de upcoming_days=15) -> 'proximo', dias=15
    obs_prox = engine_default.obligations(biz, today=date(2026, 5, 5))
    target_prox = next(o for o in obs_prox if o.fecha_limite == date(2026, 5, 20))
    assert target_prox.estado == "proximo"
    assert target_prox.dias == 15

    # 4. Faltan 16 días (> 15) -> 'aldia', dias=16
    obs_aldia = engine_default.obligations(biz, today=date(2026, 5, 4))
    target_aldia = next(o for o in obs_aldia if o.fecha_limite == date(2026, 5, 20))
    assert target_aldia.estado == "aldia"
    assert target_aldia.dias == 16

    # B. upcoming_days personalizado = 25
    engine_custom = TaxCalendarEngine(db, upcoming_days=25)
    # Con today 2026-05-04 (16 días antes), ahora debe ser 'proximo' porque 16 <= 25
    obs_custom_prox = engine_custom.obligations(biz, today=date(2026, 5, 4))
    target_custom_prox = next(o for o in obs_custom_prox if o.fecha_limite == date(2026, 5, 20))
    assert target_custom_prox.estado == "proximo"
    assert target_custom_prox.dias == 16

    # Con today 2026-04-20 (30 días antes), debe ser 'aldia' porque 30 > 25
    obs_custom_aldia = engine_custom.obligations(biz, today=date(2026, 4, 20))
    target_custom_aldia = next(o for o in obs_custom_aldia if o.fecha_limite == date(2026, 5, 20))
    assert target_custom_aldia.estado == "aldia"
    assert target_custom_aldia.dias == 30


def test_calendar_not_loaded_error(db):
    """Lanza CalendarNotLoadedError si no hay filas del año consultado."""
    # DB completamente vacía
    engine = TaxCalendarEngine(db)
    biz = _make_business()

    with pytest.raises(CalendarNotLoadedError) as exc_info:
        engine.obligations(biz, today=date(2026, 5, 1))
    assert "2026" in str(exc_info.value)

    # Ahora sembramos 2026, pero consultamos 2025
    _seed_calendar_2026(db)
    with pytest.raises(CalendarNotLoadedError) as exc_info_2025:
        engine.obligations(biz, today=date(2025, 5, 1))
    assert "2025" in str(exc_info_2025.value)


def test_manual_sales_returns_empty_list_without_error(db):
    """Un negocio MANUAL_SALES retorna lista vacía sin error y sin consultar la base."""
    # DB vacía sin calendario
    engine = TaxCalendarEngine(db)
    biz_manual = _make_business(income_source=INCOME_SOURCE_MANUAL_SALES)

    obs = engine.obligations(biz_manual, today=date(2026, 5, 1))
    assert obs == []


def test_previous_year_obligations_crossing_year_boundary(db):
    """Incluye obligaciones del año anterior solo si vencen hoy o después."""
    # Sembramos 2026 (que tiene Nov–Dic 2026 con deadline 2027-01-20)
    _seed_calendar_2026(db)

    # Sembramos además una fila para 2027 para que el año 2027 esté cargado
    db.add(
        DIANTaxCalendar(
            tax_type="IVA_BIMESTRAL",
            fiscal_year=2027,
            period_label="Ene – Feb 2027",
            period_start=date(2027, 1, 1),
            period_end=date(2027, 2, 28),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=0,
            deadline_date=date(2027, 3, 20),
            description="Declaración y pago",
        )
    )
    db.commit()

    engine = TaxCalendarEngine(db)
    biz = _make_business(iva_periodicity=IVA_PERIODICITY_BIMESTRAL, taxpayer_type="OTRO")

    # Caso 1: today = 2027-01-05 (la obligación del 2026-11 a 2026-12 vence el 2027-01-20 >= 2027-01-05)
    obs_enero_5 = engine.obligations(biz, today=date(2027, 1, 5))
    labels_5 = [o.etiqueta for o in obs_enero_5]
    assert "Nov – Dic 2026" in labels_5
    assert "Ene – Feb 2027" in labels_5

    # Caso 2: today = 2027-01-25 (ya venció el 2027-01-20 < 2027-01-25)
    # Las del año anterior que ya vencieron NO se incluyen
    obs_enero_25 = engine.obligations(biz, today=date(2027, 1, 25))
    labels_25 = [o.etiqueta for o in obs_enero_25]
    assert "Nov – Dic 2026" not in labels_25
    assert "Ene – Feb 2027" in labels_25
