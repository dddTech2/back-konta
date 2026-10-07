"""Pruebas para Story 7.2: Acceso directo al panel web desde /dashboard (Backend AC #1 a #6)."""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api.app import app
from dian_automation.core import auth_service
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import Business, Subscription, User

T0 = datetime(2026, 10, 7, 10, 0, 0)


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


def _add_user(
    db,
    uid="cliente-1",
    *,
    role="CLIENT",
    active=True,
    chat_id=123456789,
    linked=True,
    sub_status="ACTIVO",
    nit="901234567",
):
    user = User(
        id=uid,
        email=f"{uid}@kontable.co",
        full_name=f"Usuario {uid}",
        role=role,
        is_active=active,
        telegram_chat_id=chat_id,
        is_telegram_linked=linked,
    )
    db.add(user)
    db.add(
        Business(
            id=f"biz-{uid}",
            client_id=uid,
            legal_name=f"Empresa {uid}",
            commercial_name=f"Comercial {uid}",
            nit=nit,
            dv="1",
            is_active=True,
        )
    )
    if sub_status:
        db.add(
            Subscription(
                id=f"sub-{uid}",
                client_id=uid,
                plan="TRIMESTRAL",
                discount_rate=Decimal("5.00"),
                base_price=Decimal("150000.00"),
                final_price=Decimal("142500.00"),
                start_date=date.today() - timedelta(days=10),
                cutoff_date=date.today() + timedelta(days=80),
                grace_period_end=date.today() + timedelta(days=83),
                status=sub_status,
            )
        )
    db.commit()
    return user


def _extract_token_from_link(link: str) -> str:
    marker = "#/entrar/"
    assert marker in link
    return link.split(marker)[-1]


# --------------------------------------------------------------------------- AC #1: create_dashboard_link


def test_create_dashboard_link_format_and_claims(db, clock):
    """AC #1: Devuelve URL con #/entrar/<token>, claims sub, purpose, cid, iat, exp (24h)."""
    user = _add_user(db, "usr-ac1", chat_id=987654321)
    link = auth_service.create_dashboard_link(user)

    assert "#/entrar/" in link
    assert "?nit=" not in link
    token = _extract_token_from_link(link)

    claims = jwt.decode(
        token,
        auth_service._secret(),
        algorithms=[auth_service.config.jwt_algorithm],
        options={"verify_exp": False},
    )
    assert claims["sub"] == "usr-ac1"
    assert claims["purpose"] == "dashboard_link"
    assert claims["cid"] == "987654321"
    assert claims["iat"] == auth_service._epoch(T0)
    assert claims["exp"] == auth_service._epoch(T0 + timedelta(hours=24))


def test_create_dashboard_link_custom_ttl(db, clock, monkeypatch):
    """AC #1: Respeta dashboard_link_ttl_hours cuando está configurado."""
    monkeypatch.setattr(auth_service.config, "dashboard_link_ttl_hours", 48)
    user = _add_user(db, "usr-ttl", chat_id=111222)
    link = auth_service.create_dashboard_link(user)
    token = _extract_token_from_link(link)

    claims = jwt.decode(
        token,
        auth_service._secret(),
        algorithms=[auth_service.config.jwt_algorithm],
        options={"verify_exp": False},
    )
    assert claims["exp"] == auth_service._epoch(T0 + timedelta(hours=48))


def test_create_dashboard_link_handles_base_url_without_trailing_slash(db, clock, monkeypatch):
    """Dev Notes: Si kontable_web_url no termina en barra, la agrega sin duplicar barras."""
    monkeypatch.setattr(auth_service.config, "kontable_web_url", "https://konta.example.com/app")
    user = _add_user(db, "usr-url", chat_id=111222)
    link = auth_service.create_dashboard_link(user)

    assert link.startswith("https://konta.example.com/app/#/entrar/")
    assert "app//#" not in link


# --------------------------------------------------------------------------- AC #2: POST /api/auth/link-login


def test_link_login_valid_token_returns_token_response_and_usable_session(client, db, clock):
    """AC #2: Canje válido devuelve TokenResponse utilizable en /api/auth/me."""
    user = _add_user(db, "ana", chat_id=5551)
    link = auth_service.create_dashboard_link(user)
    token = _extract_token_from_link(link)

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"

    session_token = body["access_token"]
    me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {session_token}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["business_id"] == "biz-ana"


def test_link_login_reusable_multiple_times(client, db, clock):
    """AC #1 & #2: El enlace se puede usar varias veces durante su vigencia sin estado en BD."""
    user = _add_user(db, "beto", chat_id=5552)
    link = auth_service.create_dashboard_link(user)
    token = _extract_token_from_link(link)

    # Primer canje
    resp1 = client.post("/api/auth/link-login", json={"token": token})
    assert resp1.status_code == 200
    token1 = resp1.json()["access_token"]
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token1}"}).status_code == 200

    # Adelantar reloj 2 horas
    clock.advance(hours=2)

    # Segundo canje con el mismo enlace (la sesión del primero, de 60 min, ya venció)
    resp2 = client.post("/api/auth/link-login", json={"token": token})
    assert resp2.status_code == 200

    token2 = resp2.json()["access_token"]
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token2}"}).status_code == 200


def test_link_login_expired_token_is_401(client, db, clock):
    """AC #2: Enlace vencido (25 horas tras emisión) responde 401 con detalle unificado."""
    user = _add_user(db, "usr-exp", chat_id=5553)
    link = auth_service.create_dashboard_link(user)
    token = _extract_token_from_link(link)

    clock.advance(hours=25)

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_link_login_tampered_signature_is_401(client, db, clock):
    """AC #2: Firma alterada responde 401 con detalle unificado."""
    user = _add_user(db, "usr-tamper", chat_id=5554)
    link = auth_service.create_dashboard_link(user)
    token = _extract_token_from_link(link)
    tampered = token[:-4] + "wxyz"

    resp = client.post("/api/auth/link-login", json={"token": tampered})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_link_login_inactive_user_is_401(client, db, clock):
    """AC #2: Usuario inactivo responde 401 con detalle unificado."""
    user = _add_user(db, "usr-inact", chat_id=5555)
    link = auth_service.create_dashboard_link(user)
    token = _extract_token_from_link(link)

    user.is_active = False
    db.commit()

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_link_login_unlinked_telegram_is_401(client, db, clock):
    """AC #2: is_telegram_linked=False responde 401 con detalle unificado."""
    user = _add_user(db, "usr-unlink", chat_id=5556)
    link = auth_service.create_dashboard_link(user)
    token = _extract_token_from_link(link)

    user.is_telegram_linked = False
    db.commit()

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_link_login_mismatched_chat_id_is_401(client, db, clock):
    """AC #2: Re-vinculación a otro chat_id invalida el enlace viejo."""
    user = _add_user(db, "usr-relink", chat_id=5557)
    link = auth_service.create_dashboard_link(user)
    token = _extract_token_from_link(link)

    # El usuario se vincula a otro chat
    user.telegram_chat_id = 999999
    db.commit()

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_link_login_admin_user_is_401(client, db, clock):
    """AC #2: Usuario con rol ADMIN es rechazado por link-login."""
    user = _add_user(db, "admin-1", role="ADMIN", chat_id=8888)
    link = auth_service.create_dashboard_link(user)
    token = _extract_token_from_link(link)

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_link_login_nonexistent_user_is_401(client, db, clock):
    """AC #2: Usuario inexistente en BD responde 401 con detalle unificado."""
    fake_user = SimpleNamespace(id="fantasma", telegram_chat_id=123)
    link = auth_service.create_dashboard_link(fake_user)
    token = _extract_token_from_link(link)

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


@pytest.mark.parametrize("bad_token", ["", "   ", "not-a-token", "a.b", "a.b.c.d"])
def test_link_login_malformed_token_is_401(client, bad_token):
    """AC #2: Token malformado o vacío responde 401 con detalle unificado."""
    resp = client.post("/api/auth/link-login", json={"token": bad_token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_link_login_wrong_purpose_is_401(client, db, clock):
    """AC #2: Token con purpose distinto a dashboard_link responde 401."""
    user = _add_user(db, "usr-purpose", chat_id=1234)
    payload = {
        "sub": user.id,
        "purpose": "download_link",
        "cid": str(user.telegram_chat_id),
        "iat": auth_service._epoch(T0),
        "exp": auth_service._epoch(T0 + timedelta(hours=24)),
    }
    token = jwt.encode(payload, auth_service._secret(), algorithm="HS256")

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


# --------------------------------------------------------------------------- AC #3: Separación estricta de propósito


def test_session_token_rejected_by_link_login(client, db, clock):
    """AC #3: link-login rechaza un JWT de sesión (sin purpose)."""
    user = _add_user(db, "usr-sess", chat_id=1234)
    session_token = auth_service.create_access_token(user)

    resp = client.post("/api/auth/link-login", json={"token": session_token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_dashboard_link_token_rejected_as_session_bearer(client, db, clock):
    """AC #3: get_user_from_token rechaza un JWT que tenga claim purpose."""
    user = _add_user(db, "usr-bearer", chat_id=1234)
    link = auth_service.create_dashboard_link(user)
    link_token = _extract_token_from_link(link)

    # Intentar usar el token de enlace directamente como Bearer en /api/auth/me
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {link_token}"})
    assert resp.status_code == 401


# --------------------------------------------------------------------------- AC #6: Nunca se loguea el JWT de enlace ni la URL


def test_jwt_and_link_never_logged(client, db, clock, caplog):
    """AC #6: El JWT de enlace nunca se escribe en logs (ni el enlace completo)."""
    caplog.set_level(logging.DEBUG)

    user = _add_user(db, "usr-log", chat_id=777888)
    link = auth_service.create_dashboard_link(user)
    token = _extract_token_from_link(link)

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 200

    # Intentar con token inválido
    client.post("/api/auth/link-login", json={"token": "invalido.123"})

    assert token not in caplog.text
    assert link not in caplog.text
