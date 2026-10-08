"""Pruebas unitarias para las alertas proactivas de vencimientos tributarios (Story 4.1c)."""

from datetime import date, datetime, timedelta
from typing import List, Tuple
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.core.calendar_engine import (
    CalendarNotLoadedError,
    TaxCalendarEngine,
    format_limit_date,
    tax_type_label,
)
from dian_automation.core.tax_alerts import TaxDeadlineAlerter, _escape_markdown
from dian_automation.db.models import (
    Base,
    Business,
    DIANTaxCalendar,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    IVA_PERIODICITY_BIMESTRAL,
    Subscription,
    TaxDeadlineAlert,
    User,
    MOMENT_PROXIMO,
    MOMENT_VENCE_HOY,
    ALERT_STATUS_SENT,
    ALERT_STATUS_SKIPPED,
    ALERT_SKIP_REASON_BLOQUEADO,
    ALERT_SKIP_REASON_SIN_TELEGRAM,
    ALERT_SKIP_REASON_ENVIO_FALLIDO,
)


@pytest.fixture
def db_factory():
    """Fabrica de sesiones de base de datos SQLite en memoria."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield factory
    engine.dispose()


class FakeSender:
    """Simulador de envío de mensajes de Telegram que registra llamadas."""

    def __init__(self, succeed: bool = True):
        self.succeed = succeed
        self.messages: List[Tuple[int, str]] = []

    def __call__(self, chat_id: int, text: str) -> bool:
        if not self.succeed:
            return False
        self.messages.append((chat_id, text))
        return True


class ExceptionSender:
    """Simulador de sender que lanza una excepción al intentar enviar."""

    def __call__(self, chat_id: int, text: str) -> bool:
        raise RuntimeError("Conexión con Telegram fallida")


def _seed_calendar_2026(db):
    """Siembra calendario mínimo para 2026."""
    rows = [
        DIANTaxCalendar(
            tax_type="IVA_BIMESTRAL",
            fiscal_year=2026,
            period_label="Ene – Feb 2026",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 2, 28),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=0,
            deadline_date=date(2026, 3, 20),
            description="Declaración y pago",
        ),
        DIANTaxCalendar(
            tax_type="RETEFUENTE",
            fiscal_year=2026,
            period_label="Feb 2026",
            period_start=date(2026, 2, 1),
            period_end=date(2026, 2, 28),
            key_length=1,
            key_from=0,
            key_to=9,
            installment=0,
            deadline_date=date(2026, 3, 20),
            description="Retención mensual",
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
            installment=1,
            deadline_date=date(2026, 5, 12),
            description="Declaración y pago 1a cuota",
        ),
    ]
    db.add_all(rows)
    db.commit()


def _create_user_and_business(
    db,
    user_id: str = "u-1",
    biz_id: str = "b-1",
    nit: str = "901401271",
    commercial_name: str = "Tienda Ana",
    income_source: str = INCOME_SOURCE_DIAN,
    iva_periodicity: str = IVA_PERIODICITY_BIMESTRAL,
    is_withholding_agent: bool = False,
    taxpayer_type: str = "PERSONA_JURIDICA",
    is_telegram_linked: bool = True,
    telegram_chat_id: int = 123456789,
    is_active_user: bool = True,
    is_active_biz: bool = True,
    subscription_status: str = "ACTIVO",
) -> Tuple[User, Business]:
    user = User(
        id=user_id,
        email=f"{user_id}@test.com",
        full_name="Ana Pérez",
        role="CLIENT",
        is_active=is_active_user,
        is_telegram_linked=is_telegram_linked,
        telegram_chat_id=telegram_chat_id,
    )
    biz = Business(
        id=biz_id,
        client_id=user_id,
        legal_name=f"{commercial_name} SAS",
        commercial_name=commercial_name,
        nit=nit,
        dv="1",
        taxpayer_type=taxpayer_type,
        iva_periodicity=iva_periodicity,
        is_withholding_agent=is_withholding_agent,
        income_source=income_source,
        is_active=is_active_biz,
    )
    sub = Subscription(
        id=f"sub-{user_id}",
        client_id=user_id,
        plan="TRIMESTRAL",
        discount_rate=5,
        status=subscription_status,
        start_date=date(2026, 1, 1),
        cutoff_date=date(2026, 12, 31),
        grace_period_end=date(2027, 1, 3),
        base_price=50000,
        final_price=50000,
    )
    db.add_all([user, biz, sub])
    db.commit()
    return user, biz


# ==============================================================================
# 1. Pruebas de helper tax_type_label y _escape_markdown
# ==============================================================================


def test_tax_type_label_mapping():
    assert tax_type_label("IVA_BIMESTRAL") == "Declaración de IVA"
    assert tax_type_label("IVA_CUATRIMESTRAL") == "Declaración de IVA"
    assert tax_type_label("RETEFUENTE") == "Retención en la fuente"
    assert tax_type_label("RENTA_PERSONAS_NATURALES") == "Declaración de renta"
    assert tax_type_label("RENTA_PERSONAS_JURIDICAS") == "Declaración de renta"
    assert tax_type_label("OTRO_TRIBUTO") == "OTRO_TRIBUTO"


def test_escape_markdown_helper():
    assert _escape_markdown("Tienda Simple") == "Tienda Simple"
    assert _escape_markdown("Mi_Negocio* [Especial]") == r"Mi\_Negocio\* \[Especial]"
    assert _escape_markdown("`Codigo`") == r"\`Codigo\`"


# ==============================================================================
# 2. Flujo de envío: PROXIMO (dias == upcoming_days), singular "1 día", idempotencia
# ==============================================================================


def test_proximo_alert_sent_on_entering_window(db_factory):
    """Obligación con dias == upcoming_days genera 1 aviso PROXIMO y se registra SENT."""
    db = db_factory()
    _seed_calendar_2026(db)
    _create_user_and_business(db)
    db.close()

    sender = FakeSender(succeed=True)
    alerter = TaxDeadlineAlerter(db_factory, sender=sender, upcoming_days=15)

    # Fecha límite de IVA: 2026-03-20. Si today es 2026-03-05 -> diff = 15 días
    res = alerter.run(today=date(2026, 3, 5))

    assert res["sent"] == 1
    assert res["skipped"] == 0
    assert res["errors"] == 0
    assert res["calendar_missing"] is False
    assert len(sender.messages) == 1

    chat_id, text = sender.messages[0]
    assert chat_id == 123456789
    assert "📅 *Recordatorio tributario — Tienda Ana*" in text
    assert "Tu *Declaración de IVA* (Ene – Feb 2026) vence el *20 mar 2026*, en *15 días*." in text
    assert "Escribe /vencimientos para ver tu calendario completo." in text

    # Verificar registro en base de datos
    db2 = db_factory()
    alerts = db2.query(TaxDeadlineAlert).all()
    assert len(alerts) == 1
    a = alerts[0]
    assert a.moment == MOMENT_PROXIMO
    assert a.status == ALERT_STATUS_SENT
    assert a.skip_reason is None
    assert a.sent_at is not None
    db2.close()


def test_proximo_alert_singular_one_day(db_factory):
    """Cuando falta exactamente 1 día, el mensaje dice 'en *1 día*'."""
    db = db_factory()
    _seed_calendar_2026(db)
    _create_user_and_business(db)
    db.close()

    sender = FakeSender(succeed=True)
    alerter = TaxDeadlineAlerter(db_factory, sender=sender, upcoming_days=15)

    # 1 día antes del 2026-03-20 es 2026-03-19
    res = alerter.run(today=date(2026, 3, 19))

    assert res["sent"] == 1
    chat_id, text = sender.messages[0]
    assert "en *1 día*." in text
    assert "en *1 días*" not in text


def test_idempotence_no_duplicates_on_subsequent_runs(db_factory):
    """Segunda corrida el mismo día o días siguientes dentro de la ventana no reenvía."""
    db = db_factory()
    _seed_calendar_2026(db)
    _create_user_and_business(db)
    db.close()

    sender = FakeSender(succeed=True)
    alerter = TaxDeadlineAlerter(db_factory, sender=sender, upcoming_days=15)

    # Primera corrida el 2026-03-05 (dias = 15)
    res1 = alerter.run(today=date(2026, 3, 5))
    assert res1["sent"] == 1
    assert len(sender.messages) == 1

    # Segunda corrida el mismo día (mismo proceso o siguiente minuto)
    res2 = alerter.run(today=date(2026, 3, 5))
    assert res2["sent"] == 0
    assert res2["skipped"] == 0
    assert len(sender.messages) == 1

    # Tercera corrida al día siguiente (2026-03-06, dias = 14)
    res3 = alerter.run(today=date(2026, 3, 6))
    assert res3["sent"] == 0
    assert res3["skipped"] == 0
    assert len(sender.messages) == 1


# ==============================================================================
# 3. Flujo VENCE_HOY (dias == 0) tras haber enviado PROXIMO
# ==============================================================================


def test_vence_hoy_alert_sent_even_if_proximo_was_already_sent(db_factory):
    """dias == 0 genera aviso VENCE_HOY aunque ya se haya enviado PROXIMO previamente."""
    db = db_factory()
    _seed_calendar_2026(db)
    _create_user_and_business(db)
    db.close()

    sender = FakeSender(succeed=True)
    alerter = TaxDeadlineAlerter(db_factory, sender=sender, upcoming_days=15)

    # 1. Corrida en ventana previa
    alerter.run(today=date(2026, 3, 5))
    assert len(sender.messages) == 1

    # 2. Corrida el día exacto del vencimiento: 2026-03-20 (dias = 0)
    res_today = alerter.run(today=date(2026, 3, 20))
    assert res_today["sent"] == 1
    assert len(sender.messages) == 2

    chat_id, text_today = sender.messages[1]
    assert "🚨 *¡Hoy vence un impuesto! — Tienda Ana*" in text_today
    assert "Tu *Declaración de IVA* (Ene – Feb 2026) vence *hoy, 20 mar 2026*." in text_today
    assert "Si ya la presentaste, ignora este mensaje." in text_today

    # Segunda corrida el mismo día no duplica
    res_repeat = alerter.run(today=date(2026, 3, 20))
    assert res_repeat["sent"] == 0
    assert len(sender.messages) == 2

    # Verificar que existan 2 registros (uno por momento)
    db2 = db_factory()
    alerts = db2.query(TaxDeadlineAlert).all()
    assert len(alerts) == 2
    moments = {a.moment for a in alerts}
    assert moments == {MOMENT_PROXIMO, MOMENT_VENCE_HOY}
    db2.close()


# ==============================================================================
# 4. Recuperación: primera corrida con dias == 5
# ==============================================================================


def test_recovery_alert_sent_when_first_run_at_five_days(db_factory):
    """Si el alertador corre por primera vez cuando dias == 5, se genera PROXIMO."""
    db = db_factory()
    _seed_calendar_2026(db)
    _create_user_and_business(db)
    db.close()

    sender = FakeSender(succeed=True)
    alerter = TaxDeadlineAlerter(db_factory, sender=sender, upcoming_days=15)

    # 5 días antes de 2026-03-20 es 2026-03-15
    res = alerter.run(today=date(2026, 3, 15))
    assert res["sent"] == 1
    assert "en *5 días*." in sender.messages[0][1]


# ==============================================================================
# 5. Fuera de ventana (aldia) y vencidas (completado) no generan alerta
# ==============================================================================


def test_no_alert_when_outside_upcoming_window_or_already_passed(db_factory):
    """Obligaciones fuera de ventana (dias > upcoming_days) o ya vencidas no generan aviso."""
    db = db_factory()
    _seed_calendar_2026(db)
    _create_user_and_business(db)
    db.close()

    sender = FakeSender(succeed=True)
    alerter = TaxDeadlineAlerter(db_factory, sender=sender, upcoming_days=15)

    # 1. Fuera de ventana: today = 2026-02-01 (faltan 47 días para el 2026-03-20)
    res_far = alerter.run(today=date(2026, 2, 1))
    assert res_far["sent"] == 0
    assert res_far["skipped"] == 0
    assert len(sender.messages) == 0

    # 2. Vencidas: today = 2026-03-25 (venció hace 5 días)
    res_passed = alerter.run(today=date(2026, 3, 25))
    assert res_passed["sent"] == 0
    assert res_passed["skipped"] == 0
    assert len(sender.messages) == 0


# ==============================================================================
# 6. Negocios MANUAL_SALES o inactivos no se consideran
# ==============================================================================


def test_manual_sales_and_inactive_businesses_ignored(db_factory):
    """Negocios MANUAL_SALES o inactivos no generan ninguna alerta."""
    db = db_factory()
    _seed_calendar_2026(db)
    # Negocio manual
    _create_user_and_business(
        db, user_id="u-manual", biz_id="b-manual", income_source=INCOME_SOURCE_MANUAL_SALES
    )
    # Negocio inactivo
    _create_user_and_business(
        db, user_id="u-inactivo", biz_id="b-inactivo", is_active_biz=False, telegram_chat_id=123456790
    )
    db.close()

    sender = FakeSender(succeed=True)
    alerter = TaxDeadlineAlerter(db_factory, sender=sender, upcoming_days=15)

    res = alerter.run(today=date(2026, 3, 5))
    assert res["sent"] == 0
    assert res["skipped"] == 0
    assert len(sender.messages) == 0


# ==============================================================================
# 7. Omisiones (SKIPPED): sin Telegram y bloqueo de suscripción, con recuperación
# ==============================================================================


def test_skip_reason_sin_telegram_and_subsequent_link_sends(db_factory):
    """Sin Telegram vinculado genera SKIPPED/SIN_TELEGRAM; al vincularse se envía y pasa a SENT."""
    db = db_factory()
    _seed_calendar_2026(db)
    _create_user_and_business(db, is_telegram_linked=False, telegram_chat_id=None)
    db.close()

    sender = FakeSender(succeed=True)
    alerter = TaxDeadlineAlerter(db_factory, sender=sender, upcoming_days=15)

    # Primera corrida: sin Telegram vinculado
    res1 = alerter.run(today=date(2026, 3, 5))
    assert res1["sent"] == 0
    assert res1["skipped"] == 1
    assert len(sender.messages) == 0

    db2 = db_factory()
    alert = db2.query(TaxDeadlineAlert).first()
    assert alert.status == ALERT_STATUS_SKIPPED
    assert alert.skip_reason == ALERT_SKIP_REASON_SIN_TELEGRAM
    assert alert.sent_at is None

    # Cliente vincula Telegram
    user = db2.query(User).filter(User.id == "u-1").first()
    user.is_telegram_linked = True
    user.telegram_chat_id = 987654321
    db2.commit()
    db2.close()

    # Segunda corrida: cliente ya vinculado
    res2 = alerter.run(today=date(2026, 3, 6))
    assert res2["sent"] == 1
    assert res2["skipped"] == 0
    assert len(sender.messages) == 1

    # Misma fila actualizada
    db3 = db_factory()
    alerts = db3.query(TaxDeadlineAlert).all()
    assert len(alerts) == 1
    assert alerts[0].status == ALERT_STATUS_SENT
    assert alerts[0].skip_reason is None
    assert alerts[0].sent_at is not None
    db3.close()


def test_skip_reason_bloqueado_and_subsequent_unblock_sends(db_factory):
    """Suscripción bloqueada genera SKIPPED/BLOQUEADO; al reactivarse se envía."""
    db = db_factory()
    _seed_calendar_2026(db)
    _create_user_and_business(db, subscription_status="BLOQUEADO")
    db.close()

    sender = FakeSender(succeed=True)
    alerter = TaxDeadlineAlerter(db_factory, sender=sender, upcoming_days=15)

    res1 = alerter.run(today=date(2026, 3, 5))
    assert res1["sent"] == 0
    assert res1["skipped"] == 1

    db2 = db_factory()
    alert = db2.query(TaxDeadlineAlert).first()
    assert alert.status == ALERT_STATUS_SKIPPED
    assert alert.skip_reason == ALERT_SKIP_REASON_BLOQUEADO

    # Se desbloquea la suscripción
    sub = db2.query(Subscription).filter(Subscription.client_id == "u-1").first()
    sub.status = "ACTIVO"
    db2.commit()
    db2.close()

    # Siguiente corrida envía exitosamente
    res2 = alerter.run(today=date(2026, 3, 5))
    assert res2["sent"] == 1
    assert len(sender.messages) == 1


# ==============================================================================
# 8. Fallo de envío por Telegram (SKIPPED/ENVIO_FALLIDO) y reintento
# ==============================================================================


def test_telegram_send_failure_records_skip_and_retries_successfully(db_factory):
    """Fallo en sender registra ENVIO_FALLIDO y se reintenta exitosamente después."""
    db = db_factory()
    _seed_calendar_2026(db)
    _create_user_and_business(db)
    db.close()

    # Sender que devuelve False
    failing_sender = FakeSender(succeed=False)
    alerter = TaxDeadlineAlerter(db_factory, sender=failing_sender, upcoming_days=15)

    res1 = alerter.run(today=date(2026, 3, 5))
    assert res1["sent"] == 0
    assert res1["skipped"] == 1

    db2 = db_factory()
    alert = db2.query(TaxDeadlineAlert).first()
    assert alert.status == ALERT_STATUS_SKIPPED
    assert alert.skip_reason == ALERT_SKIP_REASON_ENVIO_FALLIDO
    db2.close()

    # Ahora el sender funciona
    successful_sender = FakeSender(succeed=True)
    alerter.sender = successful_sender

    res2 = alerter.run(today=date(2026, 3, 5))
    assert res2["sent"] == 1
    assert len(successful_sender.messages) == 1

    db3 = db_factory()
    alert_updated = db3.query(TaxDeadlineAlert).first()
    assert alert_updated.status == ALERT_STATUS_SENT
    assert alert_updated.skip_reason is None
    db3.close()


def test_telegram_sender_exception_does_not_halt_other_clients(db_factory):
    """Una excepción al enviar a un cliente registra ENVIO_FALLIDO y no detiene a otros."""
    db = db_factory()
    _seed_calendar_2026(db)
    _create_user_and_business(db, user_id="u-err", biz_id="b-err", telegram_chat_id=111)
    _create_user_and_business(db, user_id="u-ok", biz_id="b-ok", telegram_chat_id=222)
    db.close()

    sent_calls = []

    def selective_sender(chat_id: int, text: str) -> bool:
        sent_calls.append(chat_id)
        if chat_id == 111:
            raise RuntimeError("Telegram network timeout")
        return True

    alerter = TaxDeadlineAlerter(db_factory, sender=selective_sender, upcoming_days=15)
    res = alerter.run(today=date(2026, 3, 5))

    assert res["sent"] == 1
    assert res["skipped"] == 1
    assert 111 in sent_calls
    assert 222 in sent_calls


# ==============================================================================
# 9. Calendario no cargado: notificación a Tech Ops una sola vez por día
# ==============================================================================


def test_calendar_not_loaded_notifies_tech_ops_once_per_day(db_factory):
    """CalendarNotLoadedError avisa una vez por día a Soporte TI y termina limpiamente."""
    db = db_factory()
    # Sin calendario sembrado
    _create_user_and_business(db)
    db.close()

    tech_ops_notices = []

    def mock_tech_ops(session, msg: str):
        tech_ops_notices.append(msg)

    alerter = TaxDeadlineAlerter(
        db_factory,
        sender=FakeSender(),
        upcoming_days=15,
        tech_ops_notifier=mock_tech_ops,
    )

    # Corrida 1: año 2026 sin calendario
    res1 = alerter.run(today=date(2026, 3, 5))
    assert res1["calendar_missing"] is True
    assert res1["sent"] == 0
    assert len(tech_ops_notices) == 1
    assert "El calendario tributario de 2026 no está cargado" in tech_ops_notices[0]
    assert "python -m dian_automation.core.calendar_loader <csv>" in tech_ops_notices[0]

    # Corrida 2 el mismo día: NO vuelve a notificar a tech ops
    res2 = alerter.run(today=date(2026, 3, 5))
    assert res2["calendar_missing"] is True
    assert len(tech_ops_notices) == 1


# ==============================================================================
# 10. Escape de Markdown en nombre de negocio y etiqueta con cuotas
# ==============================================================================


def test_escaped_markdown_in_business_name_and_cuota_labels(db_factory):
    """El nombre comercial con caracteres especiales de Markdown se escapa en el texto."""
    db = db_factory()
    _seed_calendar_2026(db)
    # Negocio persona jurídica con RENTA cuota 1
    _create_user_and_business(
        db,
        commercial_name="Studio_Arte *VIP*",
        taxpayer_type="PERSONA_JURIDICA",
        iva_periodicity=None,
    )
    db.close()

    sender = FakeSender(succeed=True)
    alerter = TaxDeadlineAlerter(db_factory, sender=sender, upcoming_days=15)

    # Renta vence el 2026-05-12. Corrida el 2026-05-02 (dias = 10)
    res = alerter.run(today=date(2026, 5, 2))
    assert res["sent"] == 1

    chat_id, text = sender.messages[0]
    assert r"Studio\_Arte \*VIP\*" in text
    assert "Tu *Declaración de renta* (Renta año gravable 2025 · Cuota 1)" in text
