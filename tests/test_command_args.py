"""Separadores `|`, `,` y `;` en los comandos de Telegram."""

from datetime import date
from decimal import Decimal
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import Base
from dian_automation.telegram.admin_bot import AdminTelegramBot
from dian_automation.telegram.client_bot import ClientTelegramBot
from dian_automation.telegram.command_args import (
    split_args,
    split_first,
    split_trailing_date,
)


def test_split_args_accepts_pipe_comma_and_semicolon():
    expected = ["901008579", "CON_IVA"]
    assert split_args("901008579 | CON_IVA") == expected
    assert split_args("901008579, CON_IVA") == expected
    assert split_args("901008579;CON_IVA") == expected
    assert split_args("901008579 ; CON_IVA") == expected


def test_comma_between_digits_is_part_of_the_value():
    # Un monto con comas de miles no se parte
    assert split_args("901008579, 712,215, TR-1") == ["901008579", "712,215", "TR-1"]
    assert split_args("901008579; 712,215; TR-1") == ["901008579", "712,215", "TR-1"]


def test_pipe_keeps_commas_and_semicolons_inside_values():
    assert split_args("EMPRESA | Ana | 3001234567 | Inversiones X, S.A.S.; sede 2") == [
        "EMPRESA", "Ana", "3001234567", "Inversiones X, S.A.S.; sede 2",
    ]


def test_split_first():
    assert split_first("150000 | 3 tortas, 2 panes") == ("150000 ", " 3 tortas, 2 panes")
    assert split_first("150000, 3 tortas, 2 panes") == ("150000", " 3 tortas, 2 panes")
    assert split_first("150000; 3 tortas") == ("150000", " 3 tortas")
    assert split_first("150,000") == ("150,000", None)
    assert split_first("150000") == ("150000", None)


def test_venta_with_comma_or_semicolon():
    parse = ClientTelegramBot.parse_registrar_venta_command
    assert parse("/venta 150000, 3 tortas, 2 panes") == ("150000", " 3 tortas, 2 panes")
    assert parse("/venta 150000; 3 tortas") == ("150000", " 3 tortas")
    # Un total con coma de miles no se convierte en $150: queda entero y lo valida el servicio
    assert parse("/venta 150,000") == ("150,000", None)
    # Fecha final se quita de la descripción
    assert parse("/venta 150000 | tortas | 28/10") == ("150000", " tortas")
    assert parse("/venta 150000, 28/10") == ("150000", None)


def test_split_trailing_date_with_pipe_comma_and_semicolon():
    ref_today = date(2026, 11, 14)
    expected_date = date(2026, 10, 28)

    # Con pipe |
    resto, d = split_trailing_date("150000 | tortas | 28/10", today=ref_today)
    assert resto.strip() == "150000 | tortas"
    assert d == expected_date

    # Con coma ,
    resto, d = split_trailing_date("150000, tortas, 28/10", today=ref_today)
    assert resto.strip() == "150000, tortas"
    assert d == expected_date

    # Con punto y coma ;
    resto, d = split_trailing_date("150000; tortas; 28/10", today=ref_today)
    assert resto.strip() == "150000; tortas"
    assert d == expected_date

    # Sin descripción (solo total y fecha)
    resto, d = split_trailing_date("150000, 28/10", today=ref_today)
    assert resto.strip() == "150000"
    assert d == expected_date

    resto, d = split_trailing_date("150000 | 28/10", today=ref_today)
    assert resto.strip() == "150000"
    assert d == expected_date

    # Con formato ISO AAAA-MM-DD
    resto, d = split_trailing_date("80000; arriendo local; 2026-10-31", today=ref_today)
    assert resto.strip() == "80000; arriendo local"
    assert d == date(2026, 10, 31)

    # Con formato DD/MM/AAAA
    resto, d = split_trailing_date("80000 | arriendo local | 31/10/2026", today=ref_today)
    assert resto.strip() == "80000 | arriendo local"
    assert d == date(2026, 10, 31)


def test_split_trailing_date_inferred_year():
    # Caso 1: Mes anterior dentro del mismo año (noviembre -> octubre del año actual)
    today_nov = date(2026, 11, 14)
    resto, d = split_trailing_date("150000 | 28/10", today=today_nov)
    assert d == date(2026, 10, 28)

    # Caso 2: Enero -> fecha de diciembre queda en el año anterior
    today_jan = date(2026, 1, 15)
    resto, d = split_trailing_date("150000 | 28/12", today=today_jan)
    assert d == date(2025, 12, 28)

    # Caso 3: Mismo mes pero día futuro (ej. hoy 10 de marzo, fecha 25/03) -> año anterior
    today_mar = date(2026, 3, 10)
    resto, d = split_trailing_date("150000 | 25/03", today=today_mar)
    assert d == date(2025, 3, 25)


def test_split_trailing_date_thousands_comma_is_not_date():
    today = date(2026, 11, 14)
    # 712,215 no tiene / ni -, nunca es fecha
    resto, d = split_trailing_date("712,215", today=today)
    assert resto == "712,215"
    assert d is None

    resto, d = split_trailing_date("150000, 712,215", today=today)
    assert resto == "150000, 712,215"
    assert d is None

    resto, d = split_trailing_date("901008579, 712,215, TR-1", today=today)
    assert resto == "901008579, 712,215, TR-1"
    assert d is None


def test_split_trailing_date_date_in_middle_is_untouched():
    today = date(2026, 11, 14)
    # Fecha en medio de la descripción no se toca y sigue siendo descripción
    resto, d = split_trailing_date("150000 | tortas del 28/10 para fiesta", today=today)
    assert resto == "150000 | tortas del 28/10 para fiesta"
    assert d is None

    resto, d = split_trailing_date("150000, tortas del 28/10 para fiesta, 29/10", today=today)
    assert resto.strip() == "150000, tortas del 28/10 para fiesta"
    assert d == date(2026, 10, 29)


def test_split_trailing_date_invalid_date_raises_value_error():
    today = date(2026, 11, 14)

    # 31/02 no existe
    with pytest.raises(ValueError):
        split_trailing_date("150000 | tortas | 31/02", today=today)

    with pytest.raises(ValueError):
        split_trailing_date("31/02", today=today)

    with pytest.raises(ValueError):
        split_trailing_date("150000, 31/02", today=today)

    # 2026-02-31 no existe
    with pytest.raises(ValueError):
        split_trailing_date("80000 | 2026-02-31", today=today)

    # 31/02/2026 no existe
    with pytest.raises(ValueError):
        split_trailing_date("80000 | 31/02/2026", today=today)


def test_confirmar_pago_with_comma_and_thousands():
    ok, data, err = AdminTelegramBot.parse_confirmar_pago_command("/confirmar_pago 901008579, 712,215, TR-1")
    assert ok, err
    assert data == {"nit": "901008579", "amount": Decimal("712215"), "reference_code": "TR-1"}

    ok, data, err = AdminTelegramBot.parse_confirmar_pago_command("/confirmar_pago 901008579; 712215; NEQUI-4411")
    assert ok, err
    assert data["amount"] == Decimal("712215") and data["reference_code"] == "NEQUI-4411"


def test_crear_cliente_with_semicolons():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    ok, data, err = AdminTelegramBot.parse_crear_cliente_command(
        "/crear_cliente PERSONA; Andrea Torres; 3001234567; 1000000001; TRIMESTRAL", db
    )
    db.close()
    assert ok, err
    assert (data["full_name"], data["phone"], data["plan"]) == ("Andrea Torres", "3001234567", "TRIMESTRAL")


def test_paquete_cambiar_tipo_and_extraccion_with_comma():
    ok, data, _ = AdminTelegramBot.parse_paquete_command("/paquete 901008579, SIN_IVA")
    assert ok and data["vat_responsible"] is False
    ok, data, _ = AdminTelegramBot.parse_cambiar_tipo_command("/cambiar_tipo 901008579; MIXTO")
    assert ok and data["income_source"] == "MIXED"
    ok, data, _ = AdminTelegramBot.parse_ejecutar_extraccion_command("/ejecutar_extraccion 901008579, 2026-08")
    assert ok and data["period"] == "2026-08"
