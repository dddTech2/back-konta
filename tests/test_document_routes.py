"""Pruebas de endpoints REST para documentos de clientes (Story 7.4a): AC #11–#13."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.api.app import app
from dian_automation.config import config
from dian_automation.core import document_service
from dian_automation.db.database import Base, get_db
from dian_automation.db.models import (
    Business,
    BusinessDocument,
    Subscription,
    User,
    DOC_TYPE_RUT,
    DOC_TYPE_CAMARA_COMERCIO,
)

PDF_SAMPLE_BYTES = b"%PDF-1.4 sample content for routes"


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def seeded_data(db_session, tmp_path, monkeypatch):
    docs_dir = tmp_path / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(document_service.config, "documents_dir", str(docs_dir))

    # Dueño 1 (activo, DIAN)
    u1 = User(id="usr-ana", email="ana@test.co", full_name="Ana Gomez", role="CLIENT", is_active=True)
    b1 = Business(id="biz-ana", client_id=u1.id, legal_name="Ana Gomez", commercial_name="Panaderia Ana",
                  nit="901111111", dv="1", is_active=True, income_source="DIAN")
    s1 = Subscription(id="sub-ana", client_id=u1.id, plan="TRIMESTRAL",
                      discount_rate=Decimal("5.00"), base_price=Decimal("150000.00"),
                      final_price=Decimal("142500.00"), start_date=date.today() - timedelta(days=5),
                      cutoff_date=date.today() + timedelta(days=85),
                      grace_period_end=date.today() + timedelta(days=88), status="ACTIVO")

    # Dueño 2 (activo, MANUAL_SALES)
    u2 = User(id="usr-carlos", email="carlos@test.co", full_name="Carlos Ruiz", role="CLIENT", is_active=True)
    b2 = Business(id="biz-carlos", client_id=u2.id, legal_name="Carlos Ruiz", commercial_name="Taller Carlos",
                  nit="902222222", dv="2", is_active=True, income_source="MANUAL_SALES")
    s2 = Subscription(id="sub-carlos", client_id=u2.id, plan="SEMESTRAL",
                      discount_rate=Decimal("8.00"), base_price=Decimal("300000.00"),
                      final_price=Decimal("276000.00"), start_date=date.today() - timedelta(days=5),
                      cutoff_date=date.today() + timedelta(days=175),
                      grace_period_end=date.today() + timedelta(days=178), status="ACTIVO")

    # Dueño 3 (bloqueado)
    u3 = User(id="usr-bloq", email="bloq@test.co", full_name="Pedro Bloq", role="CLIENT", is_active=True)
    b3 = Business(id="biz-bloq", client_id=u3.id, legal_name="Pedro Bloq", commercial_name="Ferreteria Bloq",
                  nit="903333333", dv="3", is_active=True, income_source="DIAN")
    s3 = Subscription(id="sub-bloq", client_id=u3.id, plan="TRIMESTRAL",
                      discount_rate=Decimal("5.00"), base_price=Decimal("150000.00"),
                      final_price=Decimal("142500.00"), start_date=date.today() - timedelta(days=95),
                      cutoff_date=date.today() - timedelta(days=5),
                      grace_period_end=date.today() - timedelta(days=2), status="BLOQUEADO")

    # Admin
    admin = User(id="usr-admin", email="katerinn@test.co", full_name="Katerinn", role="ADMIN", is_active=True)

    db_session.add_all([u1, b1, s1, u2, b2, s2, u3, b3, s3, admin])
    db_session.commit()

    # Guardar documento para Ana
    doc1 = document_service.save_document(
        db=db_session, business=b1, doc_type=DOC_TYPE_RUT, description="RUT 2026",
        filename="RUT_Ana 2026.pdf", content=PDF_SAMPLE_BYTES, uploaded_by=admin,
    )
    # Guardar documento para Carlos (MANUAL_SALES)
    doc2 = document_service.save_document(
        db=db_session, business=b2, doc_type=DOC_TYPE_CAMARA_COMERCIO, description=None,
        filename="camara_carlos.pdf", content=PDF_SAMPLE_BYTES, uploaded_by=admin,
    )
    # Guardar documento para Pedro (Bloqueado)
    doc3 = document_service.save_document(
        db=db_session, business=b3, doc_type=DOC_TYPE_RUT, description=None,
        filename="rut_pedro.pdf", content=PDF_SAMPLE_BYTES, uploaded_by=admin,
    )

    return SimpleNamespace(
        db=db_session,
        u1=u1, b1=b1, doc1=doc1,
        u2=u2, b2=b2, doc2=doc2,
        u3=u3, b3=b3, doc3=doc3,
        admin=admin,
    )


@pytest.fixture
def client_factory(seeded_data, bearer):
    def _create(user_id: str = "usr-ana"):
        def override_get_db():
            yield seeded_data.db

        app.dependency_overrides[get_db] = override_get_db
        headers = bearer(user_id) if user_id else {}
        return TestClient(app, headers=headers)

    yield _create
    app.dependency_overrides.clear()


# ==============================================================================
# AC #11: GET /api/documents/{business_id}
# ==============================================================================

def test_list_documents_success(client_factory, seeded_data):
    client = client_factory("usr-ana")
    res = client.get(f"/api/documents/{seeded_data.b1.id}")

    assert res.status_code == 200
    body = res.json()
    assert "documents" in body
    docs = body["documents"]
    assert len(docs) == 1

    doc = docs[0]
    expected_keys = {
        "id", "doc_type", "description", "original_filename",
        "content_type", "size_bytes", "created_at",
    }
    assert set(doc.keys()) == expected_keys
    # NUNCA debe exponer storage_key ni sha256
    assert "storage_key" not in doc
    assert "sha256" not in doc
    assert doc["id"] == seeded_data.doc1.id
    assert doc["doc_type"] == DOC_TYPE_RUT
    assert doc["description"] == "RUT 2026"
    assert doc["original_filename"] == "RUT_Ana 2026.pdf"
    assert doc["content_type"] == "application/pdf"
    assert doc["size_bytes"] == len(PDF_SAMPLE_BYTES)


def test_list_documents_works_for_manual_sales_business(client_factory, seeded_data):
    client = client_factory("usr-carlos")
    res = client.get(f"/api/documents/{seeded_data.b2.id}")

    assert res.status_code == 200
    docs = res.json()["documents"]
    assert len(docs) == 1
    assert docs[0]["id"] == seeded_data.doc2.id


def test_list_documents_security_checks(client_factory, seeded_data):
    # Sin sesión -> 401
    anon_client = client_factory(user_id=None)
    assert anon_client.get(f"/api/documents/{seeded_data.b1.id}").status_code == 401

    # Negocio ajeno -> 404
    carlos_client = client_factory("usr-carlos")
    assert carlos_client.get(f"/api/documents/{seeded_data.b1.id}").status_code == 404

    # Negocio inexistente -> 404
    ana_client = client_factory("usr-ana")
    assert ana_client.get("/api/documents/biz-nonexistent").status_code == 404

    # Suscripción bloqueada -> 403
    bloq_client = client_factory("usr-bloq")
    assert bloq_client.get(f"/api/documents/{seeded_data.b3.id}").status_code == 403


# ==============================================================================
# AC #12: POST /api/documents/{business_id}/{document_id}/link
# ==============================================================================

def test_generate_download_link_success(client_factory, seeded_data):
    client = client_factory("usr-ana")
    res = client.post(f"/api/documents/{seeded_data.b1.id}/{seeded_data.doc1.id}/link")

    assert res.status_code == 200
    body = res.json()
    assert "url" in body
    assert body["expires_in"] == 300
    assert body["url"].startswith("/api/documents/file/")


def test_generate_download_link_not_found(client_factory, seeded_data):
    client = client_factory("usr-ana")

    # Documento de otro negocio -> 404
    res_other = client.post(f"/api/documents/{seeded_data.b1.id}/{seeded_data.doc2.id}/link")
    assert res_other.status_code == 404

    # Documento inexistente -> 404
    res_nonexistent = client.post(f"/api/documents/{seeded_data.b1.id}/doc-fake-id/link")
    assert res_nonexistent.status_code == 404

    # Documento retirado -> 404
    document_service.retire(seeded_data.db, seeded_data.b1, 1, by_user=seeded_data.admin)
    res_retired = client.post(f"/api/documents/{seeded_data.b1.id}/{seeded_data.doc1.id}/link")
    assert res_retired.status_code == 404


def test_generate_download_link_blocked_subscription(client_factory, seeded_data):
    client = client_factory("usr-bloq")
    res = client.post(f"/api/documents/{seeded_data.b3.id}/{seeded_data.doc3.id}/link")
    assert res.status_code == 403


# ==============================================================================
# AC #13: GET /api/documents/file/{token}
# ==============================================================================

def test_download_file_direct_success(client_factory, seeded_data):
    # Generar link con sesión
    auth_client = client_factory("usr-ana")
    res_link = auth_client.post(f"/api/documents/{seeded_data.b1.id}/{seeded_data.doc1.id}/link")
    download_url = res_link.json()["url"]

    # Descarga directa SIN cabecera Authorization
    anon_client = client_factory(user_id=None)
    res_file = anon_client.get(download_url)

    assert res_file.status_code == 200
    assert res_file.content == PDF_SAMPLE_BYTES
    assert res_file.headers["content-type"] == "application/pdf"
    assert res_file.headers["cache-control"] == "private, no-store"

    # Encabezado Content-Disposition con nombre codificado y respaldo ascii
    cd = res_file.headers["content-disposition"]
    assert "inline" in cd
    assert 'filename="documento.pdf"' in cd
    expected_encoded = quote("RUT_Ana 2026.pdf")
    assert f"filename*=UTF-8''{expected_encoded}" in cd


def test_download_file_invalid_or_expired_token(client_factory, seeded_data):
    anon_client = client_factory(user_id=None)

    # Token con firma o contenido basura -> 401
    assert anon_client.get("/api/documents/file/token_falso_invalido").status_code == 401

    # Token expirado
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    expired_payload = {
        "purpose": "document_download",
        "sub": seeded_data.u1.id,
        "did": seeded_data.doc1.id,
        "exp": int(past.timestamp()),
    }
    expired_token = jwt.encode(expired_payload, document_service.config.jwt_secret, algorithm="HS256")
    assert anon_client.get(f"/api/documents/file/{expired_token}").status_code == 401


def test_download_file_retired_after_link_generation(client_factory, seeded_data):
    auth_client = client_factory("usr-ana")
    res_link = auth_client.post(f"/api/documents/{seeded_data.b1.id}/{seeded_data.doc1.id}/link")
    download_url = res_link.json()["url"]

    # Retirar documento tras emitir el enlace
    document_service.retire(seeded_data.db, seeded_data.b1, 1, by_user=seeded_data.admin)

    # Al intentar descargar -> 404
    anon_client = client_factory(user_id=None)
    assert anon_client.get(download_url).status_code == 404


def test_download_file_blocked_subscription_returns_403(client_factory, seeded_data):
    auth_client = client_factory("usr-ana")
    res_link = auth_client.post(f"/api/documents/{seeded_data.b1.id}/{seeded_data.doc1.id}/link")
    download_url = res_link.json()["url"]

    # Bloquear la suscripción de Ana
    sub = seeded_data.db.query(Subscription).filter(Subscription.client_id == seeded_data.u1.id).one()
    sub.status = "BLOQUEADO"
    seeded_data.db.commit()

    anon_client = client_factory(user_id=None)
    res_blocked = anon_client.get(download_url)
    assert res_blocked.status_code == 403
    assert res_blocked.json()["error"] == "SUBSCRIPTION_BLOCKED"
