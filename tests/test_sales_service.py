"""Pruebas del servicio compartido de ventas (Story 2.4): validación, persistencia y bloqueo."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from dian_automation.core import income_service, sales_service
from dian_automation.core.sales_service import (
    SalesError,
    find_recent_sale_by_number,
    list_month_sales,
    list_recent_sales,
    list_recent_sales_numbered,
    register_sale,
    void_sale,
)
from dian_automation.db.models import (
    Base,
    Business,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    Sale,
    Subscription,
    User,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()

    def _client(suffix: str, status: str, income_source: str = INCOME_SOURCE_MANUAL_SALES):
        user = User(id=f"usr-{suffix}", email=f"{suffix}@x.co", full_name=suffix, role="CLIENT",
                    telegram_chat_id=1000 + len(suffix), is_telegram_linked=True, is_active=True)
        biz = Business(id=f"biz-{suffix}", client_id=user.id, legal_name=suffix, commercial_name=suffix,
                       nit="901008579", dv="7", is_active=True, income_source=income_source)
        sub = Subscription(id=f"sub-{suffix}", client_id=user.id, plan="TRIMESTRAL",
                           discount_rate=Decimal("5.00"), base_price=Decimal("150000.00"),
                           final_price=Decimal("142500.00"), start_date=date.today() - timedelta(days=10),
                           cutoff_date=date.today() + timedelta(days=80),
                           grace_period_end=date.today() + timedelta(days=83), status=status)
        return [user, biz, sub]

    session.add_all(
        _client("ok", "ACTIVO")
        + _client("blocked", "BLOQUEADO")
        + _client("dian", "ACTIVO", income_source=INCOME_SOURCE_DIAN)
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _register(db, who="ok", **kwargs):
    kwargs.setdefault("recorded_via", sales_service.RECORDED_VIA_TELEGRAM)
    user = db.get(User, f"usr-{who}")
    business = db.get(Business, f"biz-{who}")
    return register_sale(db, user=user, business=business, **kwargs)


def _count(db) -> int:
    return db.query(Sale).count()


def test_valid_sale_is_persisted_with_context(db):
    sale = _register(db, total="150000", description="3 tortas de chocolate")

    assert sale.business_id == "biz-ok"
    assert sale.recorded_by_user_id == "usr-ok"
    assert sale.recorded_via == "TELEGRAM"
    assert sale.total_amount == Decimal("150000.00")
    assert isinstance(sale.total_amount, Decimal)
    assert sale.description == "3 tortas de chocolate"
    assert sale.created_at is not None


def test_recorded_via_is_parametrizable(db):
    sale = _register(db, total="10", recorded_via=sales_service.RECORDED_VIA_WEB)

    assert sale.recorded_via == "WEB"


def test_recorded_via_is_required(db):
    user, business = db.get(User, "usr-ok"), db.get(Business, "biz-ok")

    with pytest.raises(TypeError):
        register_sale(db, user=user, business=business, total="10")


def test_decimal_precision_is_exact(db):
    sale = _register(db, total="100.10")
    db.expire_all()

    assert db.get(Sale, sale.id).total_amount == Decimal("100.10")


@pytest.mark.parametrize("raw", ["0", "0.00", "-100", "-0.5"])
def test_zero_or_negative_total_is_rejected(db, raw):
    with pytest.raises(SalesError) as exc:
        _register(db, total=raw)

    assert exc.value.code == SalesError.NON_POSITIVE_TOTAL
    assert _count(db) == 0


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "abc", "$150000", "150,5", "150.000,5", "150.000", "100.125", "1e5", ".5", "100.", "١٢٣", "1" * 13, None, 1.5],
)
def test_invalid_total_format_is_rejected(db, raw):
    with pytest.raises(SalesError) as exc:
        _register(db, total=raw)

    assert exc.value.code == SalesError.INVALID_TOTAL
    assert _count(db) == 0


@pytest.mark.parametrize(
    "raw",
    [Decimal("1E+99999999"), Decimal("1E-99999999"), Decimal("-1E+99999999"), Decimal("0E+99999999"),
     Decimal("NaN"), Decimal("Infinity"), Decimal("1E+12"), Decimal("0.001")],
)
def test_decimal_with_extreme_exponent_or_non_finite_is_rejected_without_formatting(raw):
    with pytest.raises(SalesError) as exc:
        sales_service.parse_total(raw)

    assert exc.value.code == SalesError.INVALID_TOTAL


def test_decimal_range_edges_are_accepted():
    assert sales_service.parse_total(Decimal("0.01")) == Decimal("0.01")
    assert sales_service.parse_total(Decimal("999999999999.99")) == Decimal("999999999999.99")


def test_accepted_total_formats(db):
    assert sales_service.parse_total("150000") == Decimal("150000")
    assert sales_service.parse_total(" 99.5 ") == Decimal("99.5")
    assert sales_service.parse_total(Decimal("20.00")) == Decimal("20.00")
    assert sales_service.parse_total(75) == Decimal("75")


@pytest.mark.parametrize("raw, expected", [(None, None), ("", None), ("   ", None), (" a | b ", "a | b"), ("Tortas ÑU", "Tortas ÑU")])
def test_description_normalization(raw, expected):
    assert sales_service.normalize_description(raw) == expected


def test_description_over_500_is_rejected_and_500_is_accepted(db):
    sale = _register(db, total="5", description="x" * 500)
    assert len(sale.description) == 500

    with pytest.raises(SalesError) as exc:
        _register(db, total="5", description="x" * 501)

    assert exc.value.code == SalesError.DESCRIPTION_TOO_LONG
    assert _count(db) == 1


def test_blocked_client_fails_with_block_error_and_no_row(db):
    with pytest.raises(SalesError) as exc:
        _register(db, who="blocked", total="100")

    assert exc.value.code == SalesError.BUSINESS_BLOCKED
    assert _count(db) == 0


def test_check_constraint_rejects_non_positive_total_at_database_level(db):
    db.add(Sale(business_id="biz-ok", total_amount=Decimal("0"), recorded_via="TELEGRAM",
                recorded_by_user_id="usr-ok", sale_date=date(2026, 1, 1)))

    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_register_sale_now_at_0430_utc_stores_previous_day_bogota_date(db):
    # 04:30 UTC del 2026-09-21 = 23:30 Bogotá del 2026-09-20
    now = datetime(2026, 9, 21, 4, 30)
    sale = _register(db, total="50000", now=now)

    assert sale.created_at == now
    assert sale.sale_date == date(2026, 9, 20)


def test_register_sale_now_at_0510_utc_stores_same_day_bogota_date(db):
    # 05:10 UTC del 2026-09-21 = 00:10 Bogotá del 2026-09-21
    now = datetime(2026, 9, 21, 5, 10)
    sale = _register(db, total="60000", now=now)

    assert sale.created_at == now
    assert sale.sale_date == date(2026, 9, 21)


def test_dian_business_fails_with_not_manual_sales_and_no_row(db):
    with pytest.raises(SalesError) as exc:
        _register(db, who="dian", total="100")

    assert exc.value.code == SalesError.NOT_MANUAL_SALES
    assert "Tu negocio factura electrónicamente" in exc.value.message
    assert _count(db) == 0


def test_manual_sales_business_succeeds(db):
    sale = _register(db, who="ok", total="50000", description="Venta manual")

    assert sale.id is not None
    assert sale.total_amount == Decimal("50000.00")
    assert _count(db) == 1


def test_void_sale_own_sale_succeeds(db):
    user = db.get(User, "usr-ok")
    business = db.get(Business, "biz-ok")
    sale = register_sale(db, user=user, business=business, total="50000", recorded_via=sales_service.RECORDED_VIA_TELEGRAM)
    now = datetime(2026, 9, 20, 15, 30, 0)

    result = void_sale(db, user=user, business=business, sale_id=sale.id, now=now)

    assert result.id == sale.id
    assert result.voided_at == now
    assert result.voided_by_user_id == user.id

    # La fila sigue existiendo en la base de datos y conserva sus datos
    persisted = db.get(Sale, sale.id)
    assert persisted is not None
    assert persisted.voided_at == now
    assert persisted.voided_by_user_id == user.id
    assert persisted.total_amount == Decimal("50000.00")
    assert _count(db) == 1


def test_void_sale_foreign_business_raises_sale_not_found_and_does_not_modify(db):
    user_ok = db.get(User, "usr-ok")
    business_ok = db.get(Business, "biz-ok")
    sale = register_sale(db, user=user_ok, business=business_ok, total="50000", recorded_via=sales_service.RECORDED_VIA_TELEGRAM)

    user_other = User(id="usr-other", email="other@x.co", full_name="other", role="CLIENT", is_active=True)
    biz_other = Business(id="biz-other", client_id=user_other.id, legal_name="other", commercial_name="other",
                         nit="901999999", dv="3", is_active=True, income_source=INCOME_SOURCE_MANUAL_SALES)
    db.add_all([user_other, biz_other])
    db.commit()

    with pytest.raises(SalesError) as exc:
        void_sale(db, user=user_other, business=biz_other, sale_id=sale.id)

    assert exc.value.code == SalesError.SALE_NOT_FOUND
    assert exc.value.message == "No encontramos esa venta."

    # La venta no fue modificada
    persisted = db.get(Sale, sale.id)
    assert persisted.voided_at is None
    assert persisted.voided_by_user_id is None


def test_void_sale_nonexistent_id_raises_sale_not_found(db):
    user = db.get(User, "usr-ok")
    business = db.get(Business, "biz-ok")

    with pytest.raises(SalesError) as exc:
        void_sale(db, user=user, business=business, sale_id="nonexistent-sale-id")

    assert exc.value.code == SalesError.SALE_NOT_FOUND
    assert exc.value.message == "No encontramos esa venta."


def test_void_sale_twice_raises_already_voided_and_preserves_first_timestamp(db):
    user = db.get(User, "usr-ok")
    business = db.get(Business, "biz-ok")
    sale = register_sale(db, user=user, business=business, total="50000", recorded_via=sales_service.RECORDED_VIA_TELEGRAM)
    first_now = datetime(2026, 9, 20, 10, 0, 0)
    void_sale(db, user=user, business=business, sale_id=sale.id, now=first_now)

    second_now = datetime(2026, 9, 20, 12, 0, 0)
    with pytest.raises(SalesError) as exc:
        void_sale(db, user=user, business=business, sale_id=sale.id, now=second_now)

    assert exc.value.code == SalesError.ALREADY_VOIDED
    assert exc.value.message == "Esa venta ya está anulada."

    persisted = db.get(Sale, sale.id)
    assert persisted.voided_at == first_now


def test_void_sale_dian_business_raises_not_manual_sales(db):
    user_dian = db.get(User, "usr-dian")
    biz_dian = db.get(Business, "biz-dian")

    with pytest.raises(SalesError) as exc:
        void_sale(db, user=user_dian, business=biz_dian, sale_id="any-id")

    assert exc.value.code == SalesError.NOT_MANUAL_SALES
    assert "Tu negocio factura electrónicamente" in exc.value.message


def test_void_sale_blocked_subscription_raises_business_blocked_and_changes_nothing(db):
    user_blocked = db.get(User, "usr-blocked")
    biz_blocked = db.get(Business, "biz-blocked")

    sale = Sale(
        business_id=biz_blocked.id,
        total_amount=Decimal("80000.00"),
        recorded_via="TELEGRAM",
        recorded_by_user_id=user_blocked.id,
        sale_date=date(2026, 9, 20),
        created_at=datetime(2026, 9, 20, 10, 0, 0),
    )
    db.add(sale)
    db.commit()

    with pytest.raises(SalesError) as exc:
        void_sale(db, user=user_blocked, business=biz_blocked, sale_id=sale.id)

    assert exc.value.code == SalesError.BUSINESS_BLOCKED
    assert "Suscripción suspendida por pago pendiente" in exc.value.message

    persisted = db.get(Sale, sale.id)
    assert persisted.voided_at is None
    assert persisted.voided_by_user_id is None


def test_void_sale_previous_month_works(db):
    user = db.get(User, "usr-ok")
    business = db.get(Business, "biz-ok")
    august_dt = datetime(2026, 8, 15, 12, 0, 0)
    sale = register_sale(
        db,
        user=user,
        business=business,
        total="75000",
        recorded_via=sales_service.RECORDED_VIA_TELEGRAM,
        now=august_dt,
    )
    assert sale.sale_date == date(2026, 8, 15)

    september_dt = datetime(2026, 9, 20, 16, 0, 0)
    voided = void_sale(db, user=user, business=business, sale_id=sale.id, now=september_dt)

    assert voided.voided_at == september_dt
    assert voided.sale_date == date(2026, 8, 15)
    assert db.get(Sale, sale.id).voided_at == september_dt


def test_list_recent_sales_returns_max_10_most_recent_first_excludes_voided_and_isolates_businesses(db):
    user_ok = db.get(User, "usr-ok")
    biz_ok = db.get(Business, "biz-ok")

    sales_ok = []
    base_time = datetime(2026, 9, 20, 8, 0, 0)
    for i in range(12):
        s = Sale(
            id=f"sale-ok-{i:02d}",
            business_id=biz_ok.id,
            total_amount=Decimal(f"{(i + 1) * 1000}.00"),
            recorded_via="TELEGRAM",
            recorded_by_user_id=user_ok.id,
            sale_date=date(2026, 9, 20),
            created_at=base_time + timedelta(minutes=i * 10),
        )
        sales_ok.append(s)

    voided_sale = Sale(
        id="sale-ok-voided",
        business_id=biz_ok.id,
        total_amount=Decimal("99999.00"),
        recorded_via="TELEGRAM",
        recorded_by_user_id=user_ok.id,
        sale_date=date(2026, 9, 20),
        created_at=base_time + timedelta(hours=5),
        voided_at=datetime(2026, 9, 20, 14, 0, 0),
        voided_by_user_id=user_ok.id,
    )

    user_other = User(id="usr-other-rec", email="other-rec@x.co", full_name="other", role="CLIENT", is_active=True)
    biz_other = Business(id="biz-other-rec", client_id=user_other.id, legal_name="other", commercial_name="other",
                         nit="901888777", dv="5", is_active=True, income_source=INCOME_SOURCE_MANUAL_SALES)
    sale_other = Sale(
        id="sale-other",
        business_id=biz_other.id,
        total_amount=Decimal("12345.00"),
        recorded_via="TELEGRAM",
        recorded_by_user_id=user_other.id,
        sale_date=date(2026, 9, 20),
        created_at=base_time + timedelta(hours=6),
    )

    db.add_all(sales_ok + [voided_sale, user_other, biz_other, sale_other])
    db.commit()

    results = list_recent_sales(db, biz_ok, limit=10)

    assert len(results) == 10
    assert "sale-ok-voided" not in [r.id for r in results]
    assert "sale-other" not in [r.id for r in results]
    expected_ids = [f"sale-ok-{i:02d}" for i in range(11, 1, -1)]
    assert [r.id for r in results] == expected_ids


def test_list_month_sales_filters_boundaries_excludes_voided_orders_and_validates_month(db):
    user = db.get(User, "usr-ok")
    biz = db.get(Business, "biz-ok")

    s_may_01 = Sale(
        id="s-may-01",
        business_id=biz.id,
        total_amount=Decimal("10000.00"),
        recorded_via="TELEGRAM",
        recorded_by_user_id=user.id,
        sale_date=date(2026, 5, 1),
        created_at=datetime(2026, 5, 1, 9, 0, 0),
    )
    s_may_15 = Sale(
        id="s-may-15",
        business_id=biz.id,
        total_amount=Decimal("20000.00"),
        recorded_via="TELEGRAM",
        recorded_by_user_id=user.id,
        sale_date=date(2026, 5, 15),
        created_at=datetime(2026, 5, 15, 10, 0, 0),
    )
    s_may_31 = Sale(
        id="s-may-31",
        business_id=biz.id,
        total_amount=Decimal("30000.00"),
        recorded_via="TELEGRAM",
        recorded_by_user_id=user.id,
        sale_date=date(2026, 5, 31),
        created_at=datetime(2026, 5, 31, 18, 0, 0),
    )
    s_may_voided = Sale(
        id="s-may-voided",
        business_id=biz.id,
        total_amount=Decimal("40000.00"),
        recorded_via="TELEGRAM",
        recorded_by_user_id=user.id,
        sale_date=date(2026, 5, 20),
        created_at=datetime(2026, 5, 20, 11, 0, 0),
        voided_at=datetime(2026, 5, 20, 12, 0, 0),
        voided_by_user_id=user.id,
    )
    s_apr_30 = Sale(
        id="s-apr-30",
        business_id=biz.id,
        total_amount=Decimal("50000.00"),
        recorded_via="TELEGRAM",
        recorded_by_user_id=user.id,
        sale_date=date(2026, 4, 30),
        created_at=datetime(2026, 4, 30, 23, 0, 0),
    )
    s_jun_01 = Sale(
        id="s-jun-01",
        business_id=biz.id,
        total_amount=Decimal("60000.00"),
        recorded_via="TELEGRAM",
        recorded_by_user_id=user.id,
        sale_date=date(2026, 6, 1),
        created_at=datetime(2026, 6, 1, 8, 0, 0),
    )

    db.add_all([s_may_01, s_may_15, s_may_31, s_may_voided, s_apr_30, s_jun_01])
    db.commit()

    sales = list_month_sales(db, biz, "2026-05")

    assert len(sales) == 3
    assert [s.id for s in sales] == ["s-may-31", "s-may-15", "s-may-01"]
    assert "s-may-voided" not in [s.id for s in sales]
    assert "s-apr-30" not in [s.id for s in sales]
    assert "s-jun-01" not in [s.id for s in sales]

    with pytest.raises(income_service.IncomeError) as exc:
        list_month_sales(db, biz, "2026-13")
    assert exc.value.code == income_service.IncomeError.INVALID_MONTH


def test_void_sale_atomic_update_concurrency_race_condition(db, monkeypatch):
    """Si otra petición concurrente anula la venta entre la lectura y el UPDATE, void_sale hace rollback y lanza ALREADY_VOIDED sin alterar campos."""
    user = db.get(User, "usr-ok")
    business = db.get(Business, "biz-ok")
    sale = register_sale(db, user=user, business=business, total="50000", recorded_via=sales_service.RECORDED_VIA_TELEGRAM)

    first_void_time = datetime(2026, 9, 20, 10, 0, 0)
    from sqlalchemy.orm import Query
    orig_update = Query.update

    def fake_update(self, values, *args, **kwargs):
        # Simula que otra transacción actualizó la fila en la BD antes de este UPDATE
        db.execute(
            Sale.__table__.update()
            .where(Sale.id == sale.id)
            .values(voided_at=first_void_time, voided_by_user_id="usr-other")
        )
        db.commit()
        return orig_update(self, values, *args, **kwargs)

    monkeypatch.setattr(Query, "update", fake_update)

    second_time = datetime(2026, 9, 20, 12, 0, 0)
    with pytest.raises(SalesError) as exc:
        void_sale(db, user=user, business=business, sale_id=sale.id, now=second_time)

    assert exc.value.code == SalesError.ALREADY_VOIDED
    assert exc.value.message == "Esa venta ya está anulada."

    db.expire_all()
    persisted = db.get(Sale, sale.id)
    assert persisted.voided_at == first_void_time
    assert persisted.voided_by_user_id == "usr-other"


def test_list_recent_sales_numbered_stable_numbers_and_counting_voided(db):
    """Los números son estables, crecen con el orden de creación y cuentan las anuladas."""
    user = db.get(User, "usr-ok")
    biz = db.get(Business, "biz-ok")
    t0 = datetime(2026, 9, 20, 8, 0, 0)

    s1 = register_sale(db, user=user, business=biz, total="10000", recorded_via="TELEGRAM", now=t0)
    s2 = register_sale(db, user=user, business=biz, total="20000", recorded_via="TELEGRAM", now=t0 + timedelta(minutes=10))
    s3 = register_sale(db, user=user, business=biz, total="30000", recorded_via="TELEGRAM", now=t0 + timedelta(minutes=20))

    # Crecen con el orden de creación (1, 2, 3) y se listan de la más reciente a la más antigua
    numbered = list_recent_sales_numbered(db, biz, limit=10)
    assert len(numbered) == 3
    assert [(num, s.id) for num, s in numbered] == [(3, s3.id), (2, s2.id), (1, s1.id)]

    # Anular la venta n° 2 no hace que la n° 3 pase a ser la n° 2
    void_sale(db, user=user, business=biz, sale_id=s2.id)

    numbered_after_void = list_recent_sales_numbered(db, biz, limit=10)
    assert len(numbered_after_void) == 2
    assert [(num, s.id) for num, s in numbered_after_void] == [(3, s3.id), (1, s1.id)]

    # Registrar una nueva venta no cambia el número de las ventas existentes
    s4 = register_sale(db, user=user, business=biz, total="40000", recorded_via="TELEGRAM", now=t0 + timedelta(minutes=30))

    numbered_after_add = list_recent_sales_numbered(db, biz, limit=10)
    assert len(numbered_after_add) == 3
    assert [(num, s.id) for num, s in numbered_after_add] == [(4, s4.id), (3, s3.id), (1, s1.id)]


def test_list_recent_sales_numbered_max_10_and_excludes_voided(db):
    """Muestra un máximo de 10 ventas no anuladas con su número posicional global."""
    user = db.get(User, "usr-ok")
    biz = db.get(Business, "biz-ok")
    base_time = datetime(2026, 9, 20, 8, 0, 0)

    sales = []
    for i in range(12):
        s = Sale(
            id=f"sale-numbered-{i:02d}",
            business_id=biz.id,
            total_amount=Decimal(f"{(i + 1) * 1000}.00"),
            recorded_via="TELEGRAM",
            recorded_by_user_id=user.id,
            sale_date=date(2026, 9, 20),
            created_at=base_time + timedelta(minutes=i * 10),
        )
        sales.append(s)

    # Anular la venta índice 5 (número 6)
    sales[5].voided_at = base_time + timedelta(hours=5)
    sales[5].voided_by_user_id = user.id

    db.add_all(sales)
    db.commit()

    results = list_recent_sales_numbered(db, biz, limit=10)
    # 12 ventas - 1 anulada = 11 activas. Con limit=10 devuelve 10
    assert len(results) == 10
    # Excluye la anulada
    result_ids = [s.id for _, s in results]
    assert "sale-numbered-05" not in result_ids

    # Las 10 más recientes son las ventas de índice 11 down to 1 (la 0 queda afuera por límite, la 5 por anulada)
    expected_nums = [12, 11, 10, 9, 8, 7, 5, 4, 3, 2]
    assert [num for num, _ in results] == expected_nums


def test_list_recent_sales_numbered_isolates_businesses(db):
    """Dos negocios cada uno numera desde 1 sin interferir."""
    user_ok = db.get(User, "usr-ok")
    biz_ok = db.get(Business, "biz-ok")

    user_other = User(id="usr-other-biz", email="other-biz@x.co", full_name="other", role="CLIENT", is_active=True)
    biz_other = Business(id="biz-other-biz", client_id=user_other.id, legal_name="other", commercial_name="other",
                         nit="901777888", dv="1", is_active=True, income_source=INCOME_SOURCE_MANUAL_SALES)
    db.add_all([user_other, biz_other])
    db.commit()

    t = datetime(2026, 9, 20, 9, 0, 0)
    sale_ok = register_sale(db, user=user_ok, business=biz_ok, total="15000", recorded_via="TELEGRAM", now=t)
    sale_other = register_sale(db, user=user_other, business=biz_other, total="25000", recorded_via="TELEGRAM", now=t)

    numbered_ok = list_recent_sales_numbered(db, biz_ok)
    numbered_other = list_recent_sales_numbered(db, biz_other)

    assert len(numbered_ok) == 1
    assert numbered_ok[0][0] == 1
    assert numbered_ok[0][1].id == sale_ok.id

    assert len(numbered_other) == 1
    assert numbered_other[0][0] == 1
    assert numbered_other[0][1].id == sale_other.id


def test_list_recent_sales_numbered_tiebreak_by_id(db):
    """Desempate por id cuando created_at es exactamente el mismo."""
    user = db.get(User, "usr-ok")
    biz = db.get(Business, "biz-ok")
    t = datetime(2026, 9, 20, 10, 0, 0)

    sale_a = Sale(
        id="sale-tie-a",
        business_id=biz.id,
        total_amount=Decimal("10000.00"),
        recorded_via="TELEGRAM",
        recorded_by_user_id=user.id,
        sale_date=date(2026, 9, 20),
        created_at=t,
    )
    sale_b = Sale(
        id="sale-tie-b",
        business_id=biz.id,
        total_amount=Decimal("20000.00"),
        recorded_via="TELEGRAM",
        recorded_by_user_id=user.id,
        sale_date=date(2026, 9, 20),
        created_at=t,
    )
    db.add_all([sale_a, sale_b])
    db.commit()

    numbered = list_recent_sales_numbered(db, biz, limit=10)
    # sale_a tiene menor id que sale_b, por ende sale_a es número 1 y sale_b es número 2
    # La lista ordenada por (created_at desc, id desc) muestra sale_b primero (num 2), sale_a segundo (num 1)
    assert [(num, s.id) for num, s in numbered] == [(2, "sale-tie-b"), (1, "sale-tie-a")]


def test_find_recent_sale_by_number(db):
    """find_recent_sale_by_number devuelve la venta correcta o None si está fuera de rango, anulada, inválida o de otro negocio."""
    user = db.get(User, "usr-ok")
    biz = db.get(Business, "biz-ok")
    base_time = datetime(2026, 9, 20, 8, 0, 0)

    sales = []
    for i in range(12):
        s = Sale(
            id=f"sale-find-{i:02d}",
            business_id=biz.id,
            total_amount=Decimal(f"{(i + 1) * 1000}.00"),
            recorded_via="TELEGRAM",
            recorded_by_user_id=user.id,
            sale_date=date(2026, 9, 20),
            created_at=base_time + timedelta(minutes=i * 10),
        )
        sales.append(s)

    # Anular venta índice 4 (número 5)
    sales[4].voided_at = base_time + timedelta(hours=4)
    sales[4].voided_by_user_id = user.id

    user_other = User(id="usr-find-other", email="find-other@x.co", full_name="other", role="CLIENT", is_active=True)
    biz_other = Business(id="biz-find-other", client_id=user_other.id, legal_name="other", commercial_name="other",
                         nit="901555444", dv="2", is_active=True, income_source=INCOME_SOURCE_MANUAL_SALES)
    sale_other = Sale(
        id="sale-find-other",
        business_id=biz_other.id,
        total_amount=Decimal("99000.00"),
        recorded_via="TELEGRAM",
        recorded_by_user_id=user_other.id,
        sale_date=date(2026, 9, 20),
        created_at=base_time + timedelta(minutes=5),
    )

    db.add_all(sales + [user_other, biz_other, sale_other])
    db.commit()

    # 1. Devuelve la venta correcta
    found_12 = find_recent_sale_by_number(db, biz, 12, limit=10)
    assert found_12 is not None
    assert found_12.id == "sale-find-11"

    found_2 = find_recent_sale_by_number(db, biz, 2, limit=10)
    assert found_2 is not None
    assert found_2.id == "sale-find-01"

    # 2. None para una venta anulada (número 5)
    assert find_recent_sale_by_number(db, biz, 5, limit=10) is None

    # 3. None para una venta fuera de las últimas 10 (número 1, índice 0)
    assert find_recent_sale_by_number(db, biz, 1, limit=10) is None

    # 4. None para 0 y negativos
    assert find_recent_sale_by_number(db, biz, 0, limit=10) is None
    assert find_recent_sale_by_number(db, biz, -1, limit=10) is None
    assert find_recent_sale_by_number(db, biz, -10, limit=10) is None

    # 5. None para número no existente
    assert find_recent_sale_by_number(db, biz, 99, limit=10) is None

    # 6. None para número de otro negocio
    assert find_recent_sale_by_number(db, biz_other, 12, limit=10) is None
    found_other = find_recent_sale_by_number(db, biz_other, 1, limit=10)
    assert found_other is not None
    assert found_other.id == "sale-find-other"

