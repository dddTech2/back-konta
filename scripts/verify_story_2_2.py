"""Script de demostración y verificación interactiva para Story 2.2:
Bot de Telegram para Administradora Comercial (Katerinn).
"""

import os
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dian_automation.db.models import Base, User, Business, Subscription
from dian_automation.telegram.admin_bot import AdminTelegramBot


def main():
    print("=" * 75)
    print("[VERIFICACIÓN] STORY 2.2: BOT TELEGRAM ADMINISTRADORA COMERCIAL (KATERINN)")
    print("=" * 75)

    db_filename = "verify_story_2_2.db"
    engine = create_engine(f"sqlite:///{db_filename}", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # 1. Demostración de Cálculo de Dígito de Verificación DIAN (Módulo 11)
    print("\n--- 1. VERIFICACIÓN DEL ALGORITMO DE DÍGITO DE VERIFICACIÓN (MÓDULO 11) ---")
    test_nits = [
        ("901008579", "7"),
        ("800197268", "4"),
        ("830037946", "3"),
        ("860000000", "0"),
    ]
    for nit, expected_dv in test_nits:
        computed_dv = AdminTelegramBot.calculate_dian_dv(nit)
        assert computed_dv == expected_dv, f"Error para {nit}: esperado {expected_dv}, calculado {computed_dv}"
        print(f"   [OK] NIT {nit} -> DV Calculado: {computed_dv} (Coincide con DIAN)")

    # 2. Registrar Administradora Comercial (Katerinn) y Usuario No Autorizado
    katerinn = User(
        id="usr-katerinn-admin",
        email="katerinn@kontable.co",
        full_name="Katerinn Administradora",
        role="ADMIN",
        telegram_chat_id=11223344,
        is_telegram_linked=True,
        is_active=True,
    )
    unauthorized_user = User(
        id="usr-impostor",
        email="impostor@externo.co",
        full_name="Usuario No Autorizado",
        role="CLIENT",
        telegram_chat_id=99999999,
        is_telegram_linked=True,
        is_active=True,
    )
    db.add_all([katerinn, unauthorized_user])
    db.commit()

    # 3. Prueba de Seguridad: Acceso No Autorizado
    print("\n--- 2. SEGURIDAD Y CONTROL DE ACCESO RBAC ---")
    res_unauth = AdminTelegramBot.execute_crear_cliente(
        sender_chat_id=99999999,
        text="/crear_cliente EMPRESA | Capitán Garfio | 3001112233 | Empresa Pirata SAS | 900111222 | 12345678 | TRIMESTRAL",
        db=db,
    )
    print("Intento de ejecución de usuario no admin (chat_id=99999999):")
    print(f"   Mensaje: {res_unauth['message']}")
    assert not res_unauth["success"]
    assert res_unauth["reason"] == "UNAUTHORIZED"
    print("   [OK] Acceso denegado correctamente (UNAUTHORIZED).")

    # 4. Katerinn: Validación de formato incorrecto
    print("\n--- 3. VALIDACIÓN DE FORMATO DE ENTRADA ---")
    res_bad_format = AdminTelegramBot.execute_crear_cliente(
        sender_chat_id=11223344,
        text="/crear_cliente solo_un_parametro",
        db=db,
    )
    print("Katerinn envía formato incompleto:")
    print(f"   Mensaje: {res_bad_format['message']}")
    assert not res_bad_format["success"]
    assert res_bad_format["reason"] == "INVALID_SYNTAX"
    print("   [OK] Guía de sintaxis y validación desplegada correctamente.")

    # 5. Katerinn crea clientes con diferentes planes y descuentos
    print("\n--- 4. CREACIÓN DE CLIENTES, MATRIZ DE DESCUENTOS Y ENLACES MÁGICOS ---")
    clientes_a_crear = [
        ("Andrea Torres", "Ferretería El Roble SAS", "+573001234567", "901008579", "10000002", "SEMESTRAL"),
        ("Jorge Ramírez", "Distribuciones del Valle LTDA", "+573109876543", "800197268", "10000002", "ANUAL"),
        ("Marcela Duque", "Consultores Contables SAS", "+573155554433", "830037946", "10000002", "TRIMESTRAL"),
    ]

    for contacto, nombre, tel, nit, rep_doc, plan in clientes_a_crear:
        cmd = f"/crear_cliente EMPRESA | {contacto} | {tel} | {nombre} | {nit} | {rep_doc} | {plan}"
        resp = AdminTelegramBot.execute_crear_cliente(
            sender_chat_id=11223344,
            text=cmd,
            db=db,
            bot_username="KontableColBot",
        )
        assert resp["success"], f"Error al crear cliente: {resp['message']}"
        print(f"\n[COMANDO EJECUTADO]: {cmd}")
        print("-----------------------------------------------------------------------")
        print(resp["message"])
        print("-----------------------------------------------------------------------")

    # 6. Validar datos persistidos en base de datos
    print("\n--- 5. VALIDACIÓN DE REGISTROS EN BASE DE DATOS ---")
    roble_biz = db.query(Business).filter(Business.nit == "901008579").first()
    assert roble_biz is not None
    assert roble_biz.dv == "7"
    roble_user = db.query(User).filter(User.id == roble_biz.client_id).first()
    assert roble_user is not None
    roble_sub = db.query(Subscription).filter(Subscription.client_id == roble_user.id).first()
    assert roble_sub is not None
    assert roble_sub.plan == "SEMESTRAL"
    assert roble_sub.final_price == 276000.0  # -8% descuento
    assert roble_sub.discount_rate == 8.0
    print(f"   [OK] Negocio {roble_biz.legal_name}:")
    print(f"        - NIT: {roble_biz.nit}-{roble_biz.dv}")
    print(f"        - Plan: {roble_sub.plan} (${roble_sub.final_price:,.0f} COP, -{roble_sub.discount_rate:.0f}%)")
    print(f"        - Fecha de corte: {roble_sub.cutoff_date} (Gracia hasta {roble_sub.grace_period_end})")

    # 7. Katerinn consulta el estado de clientes (/clientes)
    print("\n--- 6. CONSULTA RESUMEN DE CLIENTES (/clientes) ---")
    res_list = AdminTelegramBot.list_clients_summary(
        sender_chat_id=11223344,
        db=db,
    )
    assert res_list["success"]
    print(res_list["message"])

    # Limpieza de recursos
    db.close()
    engine.dispose()
    if os.path.exists(db_filename):
        os.remove(db_filename)

    print("\n" + "=" * 75)
    print("[ÉXITO TOTAL] TODAS LAS FUNCIONALIDADES DE LA STORY 2.2 VERIFICADAS")
    print("=" * 75)


if __name__ == "__main__":
    main()
