"""Separadores `|`, `,` y `;` en los comandos de Telegram."""

from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.models import Base
from dian_automation.telegram.admin_bot import AdminTelegramBot
from dian_automation.telegram.client_bot import ClientTelegramBot
from dian_automation.telegram.command_args import split_args, split_first


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
