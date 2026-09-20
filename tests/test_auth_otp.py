"""Pruebas de la Story 5.1: OTP por Telegram -> JWT, /api/auth/me y acceso por dueño."""

import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api.app import app
from dian_automation.core import auth_service
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import Business, OTPCode, Subscription, User
from dian_automation.telegram.client_bot import ClientTelegramBot

T0 = datetime(2026, 9, 19, 12, 0, 0)


class Clock:
    def __init__(self):
        self.now = T0

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(auth_service, "_utcnow", lambda: fake.now)
    return fake


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(db):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def sent(monkeypatch):
    """Captura los envíos de OTP (chat_id, código) sin llamar a Telegram."""
    calls = []

    def fake_send(chat_id, code):
        calls.append((chat_id, code))
        return True

    monkeypatch.setattr(ClientTelegramBot, "send_otp", staticmethod(fake_send))
    return calls


def _add_client(db, uid, *, phone=None, nit=None, chat_id=None, linked=True, sub_status="ACTIVO",
                business=True, role="CLIENT", active=True):
    db.add(User(id=uid, email=f"{uid}@x.co", full_name=uid, phone=phone, role=role,
                telegram_chat_id=chat_id, is_telegram_linked=linked, is_active=active))
    if business:
        db.add(Business(id=f"biz-{uid}", client_id=uid, legal_name=uid, commercial_name=uid,
                        nit=nit or "900000000", dv="1", is_active=True))
    if sub_status:
        db.add(Subscription(id=f"sub-{uid}", client_id=uid, plan="TRIMESTRAL", discount_rate=Decimal("5.00"),
                            base_price=Decimal("150000.00"), final_price=Decimal("142500.00"),
                            start_date=date.today() - timedelta(days=10),
                            cutoff_date=date.today() + timedelta(days=80),
                            grace_period_end=date.today() + timedelta(days=83), status=sub_status))
    db.commit()


@pytest.fixture
def ana(db):
    _add_client(db, "ana", phone="300 123 4567", nit="901234567", chat_id=5551)
    return db.get(User, "ana")


def _request(client, identifier="3001234567"):
    return client.post("/api/auth/request-otp", json={"identifier": identifier})


def _verify(client, code, identifier="3001234567"):
    return client.post("/api/auth/verify-otp", json={"identifier": identifier, "code": code})


def _rows(db, uid="ana"):
    db.expire_all()
    return db.query(OTPCode).filter(OTPCode.user_id == uid).order_by(OTPCode.created_at).all()


def _login(client, sent, identifier="3001234567"):
    assert _request(client, identifier).status_code == 202
    resp = _verify(client, sent[-1][1], identifier)
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# --------------------------------------------------------------------------- request-otp


def test_request_otp_success_persists_only_hash_and_sends_to_linked_chat(client, db, ana, sent, clock):
    resp = _request(client)

    assert resp.status_code == 202
    assert resp.json() == {"detail": "Código enviado por Telegram"}
    assert len(sent) == 1 and sent[0][0] == 5551
    code = sent[0][1]
    assert len(code) == 6 and code.isdigit()
    [row] = _rows(db)
    assert row.code_hash != code and code not in row.code_hash
    assert row.expires_at == row.created_at + timedelta(minutes=5)
    assert row.is_used is False and row.attempts == 0
    assert code not in resp.text


def test_request_otp_by_nit_and_formatted_phone(client, db, ana, sent):
    assert _request(client, "901.234.567").status_code == 202
    assert _request(client, "+300-123-4567").status_code == 202
    assert len(sent) == 2


@pytest.mark.parametrize("identifier", ["3009999999", "", "abc", "   "])
def test_request_otp_unknown_identifier_is_404_and_sends_nothing(client, db, ana, sent, identifier):
    resp = _request(client, identifier)

    assert resp.status_code == 404
    assert sent == [] and _rows(db) == []


def test_request_otp_ambiguous_identifier_is_404(client, db, ana, sent):
    _add_client(db, "beto", phone="3001234567", nit="800111222", chat_id=5552)

    resp = _request(client, "3001234567")

    assert resp.status_code == 404
    assert sent == []


def test_request_otp_ignores_non_client_roles(client, db, sent):
    _add_client(db, "admin", phone="3105550000", chat_id=9, role="ADMIN", business=False, sub_status=None)

    assert _request(client, "3105550000").status_code == 404
    assert sent == []


@pytest.mark.parametrize("linked, chat_id", [(False, None), (False, 5551), (True, None)])
def test_request_otp_without_linked_telegram_is_409(client, db, sent, linked, chat_id):
    _add_client(db, "cami", phone="3112223344", linked=linked, chat_id=chat_id)

    resp = _request(client, "3112223344")

    assert resp.status_code == 409
    assert sent == [] and _rows(db, "cami") == []


def test_request_otp_inactive_client_is_404_and_cannot_verify(client, db, sent, clock):
    _add_client(db, "inactivo", phone="3145556677", chat_id=5560)

    assert _request(client, "3145556677").status_code == 202
    code = sent[-1][1]
    db.get(User, "inactivo").is_active = False
    db.commit()

    assert _request(client, "3145556677").status_code == 404
    assert _verify(client, code, "3145556677").status_code == 401
    assert len(sent) == 1


def test_identifier_accepts_nit_with_check_digit_and_country_code_phone(client, db, ana, sent):
    assert _request(client, "901.234.567-1").status_code == 202
    assert _request(client, "+57 300 123 4567").status_code == 202
    assert _request(client, "573001234567").status_code == 202
    assert len(sent) == 3 and {chat for chat, _ in sent} == {5551}


def test_identifier_check_digit_does_not_cross_match_other_users(client, db, ana, sent):
    _add_client(db, "beto", phone="3007778888", nit="800111222", chat_id=5552)

    assert _request(client, "9012345672").status_code == 404  # DV distinto al de Ana (1)
    assert _request(client, "1" * 65).status_code == 404
    assert sent == []


def test_request_otp_rate_limit_is_three_per_ten_minutes(client, db, ana, sent, clock):
    for _ in range(3):
        assert _request(client).status_code == 202
    resp = _request(client)

    assert resp.status_code == 429
    assert len(sent) == 3 and len(_rows(db)) == 3

    clock.advance(minutes=10, seconds=1)
    assert _request(client).status_code == 202


def test_request_otp_send_failure_is_502_and_deletes_row_without_using_quota(client, db, ana, monkeypatch, clock):
    monkeypatch.setattr(ClientTelegramBot, "send_otp", staticmethod(lambda chat_id, code: False))

    for _ in range(4):
        assert _request(client).status_code == 502
    assert _rows(db) == []

    calls = []
    monkeypatch.setattr(ClientTelegramBot, "send_otp",
                        staticmethod(lambda chat_id, code: calls.append(code) or True))
    for _ in range(3):
        assert _request(client).status_code == 202


def test_request_otp_send_exception_is_502_and_deletes_row(client, db, ana, monkeypatch):
    def boom(chat_id, code):
        raise RuntimeError("red caída")

    monkeypatch.setattr(ClientTelegramBot, "send_otp", staticmethod(boom))

    assert _request(client).status_code == 502
    assert _rows(db) == []


def test_new_request_invalidates_previous_code(client, db, ana, sent, clock):
    _request(client)
    first = sent[-1][1]
    clock.advance(seconds=30)
    _request(client)
    second = sent[-1][1]

    assert _verify(client, first).status_code == 401
    assert _verify(client, second).status_code == 200


def test_send_failure_keeps_previous_code_valid(client, db, ana, sent, monkeypatch, clock):
    _request(client)
    first = sent[-1][1]
    monkeypatch.setattr(ClientTelegramBot, "send_otp", staticmethod(lambda chat_id, code: False))
    assert _request(client).status_code == 502

    assert _verify(client, first).status_code == 200


def test_request_otp_without_jwt_secret_fails_cleanly(client, db, ana, sent, jwt_test_config):
    jwt_test_config.jwt_secret = ""

    resp = _request(client)

    assert resp.status_code == 500
    assert sent == [] and _rows(db) == []


# --------------------------------------------------------------------------- verify-otp


def test_verify_otp_success_returns_bearer_token_with_ttl_and_marks_used(client, db, ana, sent, clock, jwt_test_config):
    _request(client)
    resp = _verify(client, sent[-1][1])

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer" and set(body) == {"access_token", "token_type"}
    claims = jwt.decode(body["access_token"], jwt_test_config.jwt_secret, algorithms=["HS256"],
                        options={"verify_exp": False})
    assert claims["sub"] == "ana"
    assert claims["exp"] - claims["iat"] == 60 * 60
    assert _rows(db)[0].is_used is True


def test_verify_otp_wrong_code_counts_attempt_and_does_not_consume(client, db, ana, sent, clock):
    _request(client)
    real = sent[-1][1]
    wrong = "000000" if real != "000000" else "111111"

    resp = _verify(client, wrong)

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Código inválido o expirado."}
    [row] = _rows(db)
    assert row.attempts == 1 and row.is_used is False
    assert _verify(client, real).status_code == 200


def test_verify_otp_fifth_failed_attempt_invalidates_the_code(client, db, ana, sent, clock):
    _request(client)
    real = sent[-1][1]
    wrong = "000000" if real != "000000" else "111111"

    responses = [_verify(client, wrong) for _ in range(5)]

    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1
    [row] = _rows(db)
    assert row.attempts == 5 and row.is_used is True
    correct_after = _verify(client, real)
    assert correct_after.status_code == 401 and correct_after.text == responses[0].text


def test_verify_otp_reserves_the_attempt_before_comparing_the_code(client, db, ana, sent, clock, monkeypatch):
    _request(client)
    code = sent[-1][1]
    seen = []
    original = auth_service._hash_code

    def spy(user_id, value):
        db.expire_all()
        seen.append(db.query(OTPCode).one().attempts)
        return original(user_id, value)

    monkeypatch.setattr(auth_service, "_hash_code", spy)

    assert _verify(client, code).status_code == 200
    assert seen == [1]


def test_verify_otp_expired_code_is_401(client, db, ana, sent, clock):
    _request(client)
    clock.advance(minutes=5)

    resp = _verify(client, sent[-1][1])

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Código inválido o expirado."}


def test_verify_otp_reused_code_is_401(client, db, ana, sent, clock):
    _request(client)
    code = sent[-1][1]

    assert _verify(client, code).status_code == 200
    assert _verify(client, code).status_code == 401


def test_verify_otp_concurrent_redeem_gives_a_single_token(client, db, ana, sent, clock, monkeypatch):
    """Otro canje gana la carrera entre la lectura del código y el UPDATE atómico."""
    _request(client)
    code = sent[-1][1]
    original = auth_service._hash_code

    def hash_then_lose_race(user_id, value):
        digest = original(user_id, value)
        db.execute(update(OTPCode).where(OTPCode.user_id == user_id).values(is_used=True))
        return digest

    monkeypatch.setattr(auth_service, "_hash_code", hash_then_lose_race)

    resp = _verify(client, code)

    assert resp.status_code == 401 and "access_token" not in resp.text


def test_verify_otp_foreign_or_unknown_identifier_is_401_with_same_message(client, db, ana, sent, clock):
    _add_client(db, "beto", phone="3007778888", nit="800111222", chat_id=5552)
    _request(client)
    ana_code = sent[-1][1]

    foreign = _verify(client, ana_code, identifier="3007778888")
    unknown = _verify(client, ana_code, identifier="3000000000")
    wrong = _verify(client, "abc")

    assert {foreign.status_code, unknown.status_code, wrong.status_code} == {401}
    assert foreign.text == unknown.text == wrong.text
    assert _rows(db)[0].is_used is False


def test_verify_otp_malformed_body_values_are_401_not_422(client, db, ana, sent, clock):
    _request(client)

    assert _verify(client, "").status_code == 401
    assert _verify(client, "12 34 56 78 90 12 34 56").status_code == 401


def test_blocked_business_can_still_log_in(client, db, sent, clock):
    _add_client(db, "carlos", phone="3201112233", nit="800197268", chat_id=5553, sub_status="BLOQUEADO")

    headers = _login(client, sent, "3201112233")

    assert headers["Authorization"].startswith("Bearer ")


def test_otp_code_never_appears_in_logs_or_storage(client, db, ana, sent, clock, caplog):
    caplog.set_level(logging.DEBUG)
    req = _request(client)
    code = sent[-1][1]
    ver = _verify(client, code)

    assert code not in caplog.text
    assert code not in req.text and code not in ver.text
    [row] = _rows(db)
    assert code not in " ".join(str(v) for v in (row.id, row.user_id, row.code_hash, row.expires_at))


# --------------------------------------------------------------------------- rutas protegidas


def test_protected_route_with_own_business_returns_200(client, db, ana, sent, clock):
    headers = _login(client, sent)

    assert client.get("/api/dashboard/biz-ana", headers=headers).status_code == 200
    assert client.get("/api/dashboard/901234567", headers=headers).status_code == 200
    assert client.get("/api/iva/biz-ana", headers=headers).status_code == 200
    assert client.get("/api/invoices/biz-ana", headers=headers).status_code == 200


def test_protected_route_without_session_is_401_with_www_authenticate(client, db, ana):
    resp = client.get("/api/dashboard/biz-ana")

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json() == {"detail": "Sesión inválida o expirada."}


@pytest.mark.parametrize("header", ["Bearer not-a-jwt", "Bearer ", "Basic abc", "Bearer a.b.c"])
def test_protected_route_with_malformed_credentials_is_401(client, db, ana, header):
    resp = client.get("/api/dashboard/biz-ana", headers={"Authorization": header})

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


def test_protected_route_with_expired_jwt_is_401(client, db, ana, sent, clock):
    headers = _login(client, sent)
    clock.advance(minutes=61)

    assert client.get("/api/dashboard/biz-ana", headers=headers).status_code == 401
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_jwt_signed_with_another_secret_or_without_signature_is_401(client, db, ana):
    payload = {"sub": "ana", "exp": 4102444800}
    forged = jwt.encode(payload, "otro-secreto-distinto-de-pruebas-123", algorithm="HS256")
    unsigned = jwt.encode(payload, None, algorithm="none")

    for token in (forged, unsigned):
        resp = client.get("/api/dashboard/biz-ana", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


def test_jwt_of_inactive_or_missing_user_is_401(client, db, ana, bearer):
    ana.is_active = False
    db.commit()

    assert client.get("/api/dashboard/biz-ana", headers=bearer("ana")).status_code == 401
    assert client.get("/api/dashboard/biz-ana", headers=bearer("fantasma")).status_code == 401


def test_session_is_checked_before_the_business_exists(client, db, ana):
    assert client.get("/api/dashboard/biz-inexistente").status_code == 401


def test_foreign_business_is_404_identical_to_nonexistent(client, db, ana, bearer):
    _add_client(db, "beto", phone="3007778888", nit="800111222", chat_id=5552)

    foreign = client.get("/api/dashboard/biz-beto", headers=bearer("ana"))
    foreign_by_nit = client.get("/api/dashboard/800111222", headers=bearer("ana"))
    missing = client.get("/api/dashboard/biz-beto", headers=bearer("fantasma-no-existe"))
    ghost = client.get("/api/dashboard/no-existe", headers=bearer("ana"))

    assert foreign.status_code == foreign_by_nit.status_code == ghost.status_code == 404
    assert foreign.json()["detail"].split("'")[0] == ghost.json()["detail"].split("'")[0]
    assert missing.status_code == 401


def test_blocked_business_with_valid_jwt_is_403_on_protected_routes(client, db, sent, clock):
    _add_client(db, "carlos", phone="3201112233", nit="800197268", chat_id=5553, sub_status="BLOQUEADO")
    headers = _login(client, sent, "3201112233")

    resp = client.get("/api/dashboard/biz-carlos", headers=headers)

    assert resp.status_code == 403
    assert resp.json()["error"] == "SUBSCRIPTION_BLOCKED"


def test_ownership_is_checked_before_subscription(client, db, ana, bearer):
    _add_client(db, "carlos", phone="3201112233", nit="800197268", chat_id=5553, sub_status="BLOQUEADO")

    assert client.get("/api/dashboard/biz-carlos", headers=bearer("ana")).status_code == 404


# --------------------------------------------------------------------------- /me


def test_me_for_provisioned_user(client, db, ana, bearer):
    resp = client.get("/api/auth/me", headers=bearer("ana"))

    assert resp.status_code == 200
    assert resp.json() == {
        "business_id": "biz-ana",
        "is_provisioned": True,
        "is_blocked": False,
        "subscription_status": "ACTIVO",
        "has_warning_banner": False,
        "redirect_url": None,
    }


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer x.y.z"}])
def test_me_without_valid_session_is_401(client, db, ana, headers):
    resp = client.get("/api/auth/me", headers=headers)

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


def test_me_without_business_is_not_provisioned(client, db, bearer):
    _add_client(db, "dani", phone="3134445566", business=False, sub_status=None)

    resp = client.get("/api/auth/me", headers=bearer("dani"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["business_id"] is None and body["is_provisioned"] is False
    assert body["subscription_status"] is None and body["is_blocked"] is False


def test_me_blocked_business_is_200_with_redirect(client, db, bearer):
    _add_client(db, "carlos", phone="3201112233", nit="800197268", chat_id=5553, sub_status="BLOQUEADO")

    resp = client.get("/api/auth/me", headers=bearer("carlos"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_blocked"] is True and body["subscription_status"] == "BLOQUEADO"
    assert body["redirect_url"] == "/servicio-suspendido"
    assert body["business_id"] == "biz-carlos" and body["is_provisioned"] is True


def test_me_in_grace_period_shows_warning_banner(client, db, bearer):
    _add_client(db, "mora", phone="3151112233", nit="810000001", chat_id=5570, sub_status="EN_MORA")
    sub = db.get(Subscription, "sub-mora")
    sub.cutoff_date = date.today() - timedelta(days=1)
    sub.grace_period_end = date.today() + timedelta(days=2)
    db.commit()

    body = client.get("/api/auth/me", headers=bearer("mora")).json()

    assert body["has_warning_banner"] is True and body["is_blocked"] is False
    assert body["subscription_status"] == "EN_MORA"


def test_me_reports_the_status_of_the_latest_subscription(client, db, ana, bearer):
    db.get(Subscription, "sub-ana").created_at = T0 - timedelta(days=200)
    db.add(Subscription(id="sub-ana-new", client_id="ana", plan="ANUAL", discount_rate=Decimal("10.00"),
                        base_price=Decimal("600000.00"), final_price=Decimal("540000.00"),
                        start_date=date.today(), cutoff_date=date.today() + timedelta(days=365),
                        grace_period_end=date.today() + timedelta(days=368), status="ACTIVO",
                        created_at=T0))
    db.get(Subscription, "sub-ana").status = "CANCELADO"
    db.commit()

    assert client.get("/api/auth/me", headers=bearer("ana")).json()["subscription_status"] == "ACTIVO"


def test_me_with_several_businesses_returns_the_oldest_active_one_without_fiscal_data(client, db, ana, bearer):
    db.add(Business(id="biz-ana-old", client_id="ana", legal_name="Vieja", commercial_name="Vieja",
                    nit="811111111", dv="2", is_active=True, created_at=T0 - timedelta(days=400)))
    db.add(Business(id="biz-ana-inactive", client_id="ana", legal_name="Inactiva", commercial_name="Inactiva",
                    nit="822222222", dv="3", is_active=False, created_at=T0 - timedelta(days=800)))
    db.commit()

    resp = client.get("/api/auth/me", headers=bearer("ana"))

    assert resp.json()["business_id"] == "biz-ana-old"
    assert set(resp.json()) == {"business_id", "is_provisioned", "is_blocked", "subscription_status",
                                "has_warning_banner", "redirect_url"}


# --------------------------------------------------------------------------- envío por Telegram


class _FakeResponse:
    def __init__(self, status_code=200, ok=True):
        self.status_code = status_code
        self._ok = ok

    def json(self):
        return {"ok": self._ok}


def test_send_otp_posts_to_telegram_with_short_timeout(monkeypatch):
    seen = {}

    def fake_post(url, json, timeout):
        seen.update(url=url, json=json, timeout=timeout)
        return _FakeResponse()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TOKEN")
    monkeypatch.setattr("dian_automation.telegram.client_bot.httpx.post", fake_post)

    assert ClientTelegramBot.send_otp(5551, "482913") is True
    assert seen["url"].endswith("/bot123:TOKEN/sendMessage")
    assert seen["json"]["chat_id"] == 5551 and "482913" in seen["json"]["text"]
    assert seen["timeout"] == 3.0


@pytest.mark.parametrize("response", [_FakeResponse(500), _FakeResponse(200, ok=False)])
def test_send_otp_returns_false_on_telegram_rejection(monkeypatch, response):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TOKEN")
    monkeypatch.setattr("dian_automation.telegram.client_bot.httpx.post", lambda *a, **k: response)

    assert ClientTelegramBot.send_otp(5551, "482913") is False


def test_send_otp_network_error_returns_false_and_does_not_log_token_or_code(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("https://api.telegram.org/bot123:TOKEN/sendMessage falló con 482913")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TOKEN")
    monkeypatch.setattr("dian_automation.telegram.client_bot.httpx.post", boom)
    caplog.set_level(logging.DEBUG)

    assert ClientTelegramBot.send_otp(5551, "482913") is False
    assert "TOKEN" not in caplog.text and "482913" not in caplog.text


def test_send_otp_without_bot_token_returns_false(monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ADMIN_BOT_TOKEN", "TELEGRAM_CLIENT_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    assert ClientTelegramBot.send_otp(5551, "482913") is False


def test_send_otp_accepts_the_same_token_fallbacks_as_the_bot_runner(monkeypatch):
    seen = {}
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CLIENT_BOT_TOKEN", "999:CLIENTE")
    monkeypatch.setattr("dian_automation.telegram.client_bot.httpx.post",
                        lambda url, json, timeout: seen.update(url=url) or _FakeResponse())

    assert ClientTelegramBot.send_otp(5551, "482913") is True
    assert "999:CLIENTE" in seen["url"]


def _config_ttl(env_value):
    env = {k: v for k, v in os.environ.items() if k not in ("JWT_TTL_MINUTES", "JWT_SECRET")}
    if env_value is not None:
        env["JWT_TTL_MINUTES"] = env_value
    result = subprocess.run(
        [sys.executable, "-c",
         "from dian_automation.config import config; print(config.jwt_ttl_minutes, repr(config.jwt_secret))"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


@pytest.mark.parametrize("env_value, expected", [(None, "60"), ("", "60"), ("15", "15")])
def test_config_imports_without_jwt_secret_and_tolerates_blank_ttl(env_value, expected):
    ttl, secret = _config_ttl(env_value)

    assert ttl == expected
