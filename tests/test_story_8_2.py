"""Pruebas unitarias para Story 8.2: Acceso de la administradora al panel (/panel, OTP, rol y guardas).

Cubre AC #1 a #6 del Backend:
1. create_admin_link y exchange_dashboard_link con purpose admin_link/dashboard_link y roles ADMIN/CLIENT.
2. /panel en admin_bot: Markdown link, advertencia 24h, rechazo a clientes, presencia en /ayuda.
3. request-otp y verify-otp por celular para ADMIN activo con Telegram vinculado (NIT solo clientes).
4. GET /api/auth/me agrega role ("CLIENT" | "ADMIN") y payload neutro para ADMIN (sin negocio ni suscripción).
5. get_current_admin y GET /api/admin/ping: sin sesión 401, cliente 403 {"detail": "Acceso solo para la administración."}, admin 200 {"ok": True}.
6. get_business_with_access sigue protegiendo endpoints de cliente (ADMIN recibe 404).
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

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
from dian_automation.telegram.admin_bot import AdminTelegramBot
from dian_automation.telegram.client_bot import ClientTelegramBot

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


def _seed_admin(db, uid="admin-kat", chat_id=555444333, phone="3001234567", active=True, linked=True):
    admin = User(
        id=uid,
        email=f"{uid}@kontable.co",
        full_name="Katerinn Administradora",
        role="ADMIN",
        phone=phone,
        telegram_chat_id=chat_id,
        is_telegram_linked=linked,
        is_active=active,
    )
    db.add(admin)
    db.commit()
    return admin


def _seed_client(db, uid="cliente-andrea", chat_id=777888999, phone="3109876543", nit="901008579", active=True, linked=True):
    client_user = User(
        id=uid,
        email=f"{uid}@empresa.co",
        full_name="Andrea Torres",
        role="CLIENT",
        phone=phone,
        telegram_chat_id=chat_id,
        is_telegram_linked=linked,
        is_active=active,
    )
    db.add(client_user)
    biz = Business(
        id=f"biz-{uid}",
        client_id=uid,
        legal_name=f"Empresa {uid}",
        commercial_name=f"Comercial {uid}",
        nit=nit,
        dv="7",
        income_source="DIAN",
        is_active=True,
    )
    db.add(biz)
    sub = Subscription(
        id=f"sub-{uid}",
        client_id=uid,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date.today() - timedelta(days=10),
        cutoff_date=date.today() + timedelta(days=80),
        grace_period_end=date.today() + timedelta(days=83),
        status="ACTIVO",
    )
    db.add(sub)
    db.commit()
    return client_user


def _extract_token(link: str) -> str:
    marker = "#/entrar/"
    assert marker in link
    return link.split(marker)[-1]


# ---------------------------------------------------------------------------
# AC #1: create_admin_link y exchange_dashboard_link con purpose por rol
# ---------------------------------------------------------------------------


def test_create_admin_link_format_and_claims(db, clock):
    """AC #1: create_admin_link genera URL f'{base}#/entrar/{token}' con purpose='admin_link'."""
    admin = _seed_admin(db)
    link = auth_service.create_admin_link(admin)

    assert "#/entrar/" in link
    token = _extract_token(link)

    claims = jwt.decode(
        token,
        auth_service._secret(),
        algorithms=[auth_service.config.jwt_algorithm],
        options={"verify_exp": False},
    )
    assert claims["sub"] == admin.id
    assert claims["purpose"] == "admin_link"
    assert claims["cid"] == str(admin.telegram_chat_id)
    assert claims["iat"] == auth_service._epoch(T0)
    assert claims["exp"] == auth_service._epoch(T0 + timedelta(hours=24))


def test_exchange_admin_link_by_admin_returns_session(client, db, clock):
    """AC #1: Enlace de admin canjeado por ADMIN -> sesión válida (200 en /api/auth/me con role ADMIN)."""
    admin = _seed_admin(db)
    link = auth_service.create_admin_link(admin)
    token = _extract_token(link)

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 200
    session_token = resp.json()["access_token"]

    me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {session_token}"})
    assert me_resp.status_code == 200
    body = me_resp.json()
    assert body["role"] == "ADMIN"
    assert body["business_id"] is None
    assert body["is_provisioned"] is False


def test_exchange_admin_link_with_client_user_is_401(client, db, clock):
    """AC #1: Enlace admin con usuario CLIENT en subject -> 401."""
    client_user = _seed_client(db)
    link = auth_service.create_admin_link(client_user)
    token = _extract_token(link)

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_exchange_dashboard_link_with_admin_user_is_401(client, db, clock):
    """AC #1: Enlace de cliente (dashboard_link) con usuario ADMIN -> 401."""
    admin = _seed_admin(db)
    link = auth_service.create_dashboard_link(admin)
    token = _extract_token(link)

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_admin_link_expired_is_401(client, db, clock):
    """AC #1: Enlace admin vencido responde 401."""
    admin = _seed_admin(db)
    link = auth_service.create_admin_link(admin)
    token = _extract_token(link)

    clock.advance(hours=25)

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_admin_link_inactive_admin_is_401(client, db, clock):
    """AC #1: Enlace admin para usuario dado de baja responde 401."""
    admin = _seed_admin(db)
    link = auth_service.create_admin_link(admin)
    token = _extract_token(link)

    admin.is_active = False
    db.commit()

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_admin_link_unlinked_telegram_is_401(client, db, clock):
    """AC #1: Enlace admin si se desvincula Telegram responde 401."""
    admin = _seed_admin(db)
    link = auth_service.create_admin_link(admin)
    token = _extract_token(link)

    admin.is_telegram_linked = False
    db.commit()

    resp = client.post("/api/auth/link-login", json={"token": token})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "El enlace venció o no es válido."}


def test_admin_link_reusable_multiple_times(client, db, clock):
    """AC #1: El enlace de admin puede usarse varias veces dentro de las 24 horas."""
    admin = _seed_admin(db)
    link = auth_service.create_admin_link(admin)
    token = _extract_token(link)

    resp1 = client.post("/api/auth/link-login", json={"token": token})
    assert resp1.status_code == 200

    clock.advance(hours=2)

    resp2 = client.post("/api/auth/link-login", json={"token": token})
    assert resp2.status_code == 200


# ---------------------------------------------------------------------------
# AC #2: /panel en admin_bot.py
# ---------------------------------------------------------------------------


def test_panel_command_authorized_admin(db):
    """AC #2: /panel por ADMIN autorizado devuelve Markdown link, aviso de 24h y no reenvío."""
    admin = _seed_admin(db, chat_id=555444333)

    res = AdminTelegramBot.handle_panel(sender_chat_id=555444333, db=db)
    assert res["success"] is True
    assert "link" in res
    assert "#/entrar/" in res["link"]

    msg = res["message"]
    # Enlace Markdown requerido: [Abrir panel de administración](url)
    assert f"👉 [Abrir panel de administración]({res['link']})" in msg
    assert "24 horas" in msg
    assert "No lo reenvíes" in msg


def test_panel_command_unauthorized_client_rejected(db):
    """AC #2: /panel ejecutado por un CLIENT o chat no autorizado es rechazado."""
    _seed_client(db, chat_id=777888999)

    res = AdminTelegramBot.handle_panel(sender_chat_id=777888999, db=db)
    assert res["success"] is False
    assert res["reason"] == "UNAUTHORIZED"
    assert "Acceso denegado" in res["message"]


def test_panel_command_dispatched_via_handle_admin_message(db):
    """AC #2: /panel y /panel@Bot se despachan por handle_admin_message."""
    _seed_admin(db, chat_id=555444333)

    msg = AdminTelegramBot.handle_admin_message(
        sender_chat_id=555444333,
        text="/panel",
        db=db,
    )
    assert "👉 [Abrir panel de administración]" in msg
    assert "24 horas" in msg

    # Con mención
    msg_mention = AdminTelegramBot.handle_admin_message(
        sender_chat_id=555444333,
        text="/panel@KontaBot",
        db=db,
    )
    assert "👉 [Abrir panel de administración]" in msg_mention


def test_panel_appears_in_ayuda_menu(db):
    """AC #2: /panel aparece en el menú de /ayuda del bot admin."""
    _seed_admin(db, chat_id=555444333)

    help_msg = AdminTelegramBot.handle_admin_message(
        sender_chat_id=555444333,
        text="/ayuda",
        db=db,
    )
    assert "/panel" in help_msg
    assert "Panel de Administración" in help_msg or "Panel Web" in help_msg


# ---------------------------------------------------------------------------
# AC #3: request-otp y verify-otp para ADMIN por celular
# ---------------------------------------------------------------------------


def test_resolve_client_by_identifier_finds_admin_by_phone(db):
    """AC #3: resolve_client_by_identifier encuentra ADMIN por teléfono (con y sin 57)."""
    admin = _seed_admin(db, phone="3001234567")

    # Coincidencia exacta de 10 dígitos
    assert auth_service.resolve_client_by_identifier(db, "3001234567") == admin
    # Con prefijo país 57
    assert auth_service.resolve_client_by_identifier(db, "+57 300 123 4567") == admin
    assert auth_service.resolve_client_by_identifier(db, "573001234567") == admin


def test_resolve_client_by_identifier_does_not_find_admin_by_nit(db):
    """AC #3: El NIT solo aplica a clientes; no encuentra administradores aunque se pase un NIT."""
    _seed_admin(db, phone="3001234567")
    _seed_client(db, nit="901008579")

    # El NIT encuentra al cliente, no al admin
    found = auth_service.resolve_client_by_identifier(db, "901008579")
    assert found is not None
    assert found.role == "CLIENT"


def test_resolve_client_by_identifier_ambiguous_phone_returns_none(db):
    """AC #3: Si dos usuarios distintos comparten el mismo teléfono, devuelve None."""
    _seed_admin(db, uid="adm-1", phone="3009998877")
    _seed_client(db, uid="cli-1", phone="3009998877")

    assert auth_service.resolve_client_by_identifier(db, "3009998877") is None


def test_request_and_verify_otp_for_admin_by_phone(client, db, monkeypatch):
    """AC #3: Flujo OTP completo para celular de ADMIN con Telegram vinculado."""
    admin = _seed_admin(db, phone="3001234567", chat_id=555444333, linked=True)
    sent_codes = []

    def fake_send_otp(chat_id, code):
        sent_codes.append((chat_id, code))
        return True

    monkeypatch.setattr(ClientTelegramBot, "send_otp", fake_send_otp)

    # 1. request-otp
    resp_req = client.post("/api/auth/request-otp", json={"identifier": "3001234567"})
    assert resp_req.status_code == 202
    assert len(sent_codes) == 1
    assert sent_codes[0][0] == admin.telegram_chat_id
    code = sent_codes[0][1]

    # 2. verify-otp
    resp_ver = client.post("/api/auth/verify-otp", json={"identifier": "3001234567", "code": code})
    assert resp_ver.status_code == 200
    token = resp_ver.json()["access_token"]

    # 3. sesión utilizable en /me
    me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["role"] == "ADMIN"


def test_request_otp_for_admin_without_telegram_linked_is_409(client, db):
    """AC #3: Admin sin Telegram vinculado responde 409."""
    _seed_admin(db, phone="3001234567", linked=False)

    resp = client.post("/api/auth/request-otp", json={"identifier": "3001234567"})
    assert resp.status_code == 409
    assert "Telegram vinculado" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# AC #4: GET /api/auth/me con role ('CLIENT' | 'ADMIN') y respuesta para ADMIN
# ---------------------------------------------------------------------------


def test_me_endpoint_for_admin(client, db):
    """AC #4: GET /api/auth/me para ADMIN responde role='ADMIN', business_id=null, is_provisioned=false, etc."""
    admin = _seed_admin(db)
    token = auth_service.create_access_token(admin)

    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {
        "role": "ADMIN",
        "business_id": None,
        "income_source": None,
        "is_provisioned": False,
        "is_blocked": False,
        "subscription_status": None,
        "has_warning_banner": False,
        "redirect_url": None,
    }


def test_me_endpoint_for_client_includes_role_client(client, db):
    """AC #4: GET /api/auth/me para CLIENT incluye role='CLIENT'."""
    client_user = _seed_client(db)
    token = auth_service.create_access_token(client_user)

    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "CLIENT"
    assert body["business_id"] == f"biz-{client_user.id}"
    assert body["is_provisioned"] is True


# ---------------------------------------------------------------------------
# AC #5: Guarda get_current_admin y GET /api/admin/ping
# ---------------------------------------------------------------------------


def test_admin_ping_without_session_is_401(client):
    """AC #5: GET /api/admin/ping sin sesión responde 401."""
    resp = client.get("/api/admin/ping")
    assert resp.status_code == 401


def test_admin_ping_with_client_session_is_403(client, db):
    """AC #5: GET /api/admin/ping con sesión de CLIENT responde 403 con 'Acceso solo para la administración.'."""
    client_user = _seed_client(db)
    token = auth_service.create_access_token(client_user)

    resp = client.get("/api/admin/ping", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Acceso solo para la administración."}


def test_admin_ping_with_admin_session_is_200(client, db):
    """AC #5: GET /api/admin/ping con sesión de ADMIN responde 200 {"ok": true}."""
    admin = _seed_admin(db)
    token = auth_service.create_access_token(admin)

    resp = client.get("/api/admin/ping", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_admin_ping_with_inactive_admin_session_is_401_or_403(client, db):
    """AC #5: Administrador inactivo no puede acceder a /api/admin/ping."""
    admin = _seed_admin(db, active=False)
    # Token emitido antes de inactivar
    token = auth_service.create_access_token(admin)

    resp = client.get("/api/admin/ping", headers={"Authorization": f"Bearer {token}"})
    # get_user_from_token valida is_active -> 401
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# AC #6: Endpoints de cliente (get_business_with_access) devuelven 404 para ADMIN
# ---------------------------------------------------------------------------


def test_admin_accessing_client_endpoint_returns_404(client, db):
    """AC #6: Un ADMIN que llame a un endpoint de cliente recibe 404 (no accede a negocios ajenos)."""
    admin = _seed_admin(db)
    client_user = _seed_client(db)
    token = auth_service.create_access_token(admin)

    # Intento de acceder al dashboard del negocio del cliente
    resp = client.get(
        f"/api/dashboard/biz-{client_user.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
