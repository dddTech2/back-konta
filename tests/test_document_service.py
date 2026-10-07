"""Pruebas unitarias para core/document_service.py (Story 7.4a)."""

import hashlib
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dian_automation.config import config
from dian_automation.core import document_service
from dian_automation.db.database import Base
from dian_automation.db.models import (
    Business,
    BusinessDocument,
    Subscription,
    User,
    DOC_TYPE_RUT,
    DOC_TYPE_CAMARA_COMERCIO,
    DOC_TYPE_CEDULA_REPRESENTANTE,
    DOC_TYPE_CERTIFICACION_BANCARIA,
    DOC_TYPE_OTRO,
)
from dian_automation.subscriptions.lockout_service import SubscriptionBlockedError

VALID_PDF_BYTES = b"%PDF-1.4 test document content here"
VALID_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake png raw bytes"
VALID_JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIFfake jpeg"
INVALID_EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00binary executable"


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
def test_setup(db_session, tmp_path, monkeypatch):
    """Configura documentos_dir en tmp_path y siembra un usuario y negocio de prueba."""
    docs_dir = tmp_path / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(document_service.config, "documents_dir", str(docs_dir))
    monkeypatch.setattr(document_service.config, "document_max_mb", 10)

    user = User(
        id="usr-test-1",
        email="test@user.co",
        full_name="Usuario Prueba",
        role="CLIENT",
        is_active=True,
    )
    admin = User(
        id="usr-admin-1",
        email="katerinn@kontable.co",
        full_name="Katerinn Admin",
        role="ADMIN",
        is_active=True,
    )
    business = Business(
        id="biz-test-1",
        client_id=user.id,
        legal_name="Empresa Prueba SAS",
        commercial_name="Empresa Prueba",
        nit="901008579",
        dv="7",
        is_active=True,
        income_source="DIAN",
    )
    sub = Subscription(
        id="sub-test-1",
        client_id=user.id,
        plan="TRIMESTRAL",
        discount_rate=Decimal("5.00"),
        base_price=Decimal("150000.00"),
        final_price=Decimal("142500.00"),
        start_date=date.today() - timedelta(days=5),
        cutoff_date=date.today() + timedelta(days=85),
        grace_period_end=date.today() + timedelta(days=88),
        status="ACTIVO",
    )
    db_session.add_all([user, admin, business, sub])
    db_session.commit()

    return SimpleNamespace(
        db=db_session,
        user=user,
        admin=admin,
        business=business,
        sub=sub,
        docs_dir=docs_dir,
    )


def test_save_valid_pdf_persists_disk_and_database(test_setup):
    db, biz, admin = test_setup.db, test_setup.business, test_setup.admin

    doc = document_service.save_document(
        db=db,
        business=biz,
        doc_type=DOC_TYPE_RUT,
        description=None,
        filename="rut_original.pdf",
        content=VALID_PDF_BYTES,
        uploaded_by=admin,
    )

    assert doc.id
    assert doc.business_id == biz.id
    assert doc.doc_type == DOC_TYPE_RUT
    assert doc.content_type == "application/pdf"
    assert doc.size_bytes == len(VALID_PDF_BYTES)
    assert doc.sha256 == hashlib.sha256(VALID_PDF_BYTES).hexdigest()
    assert doc.original_filename == "rut_original.pdf"
    assert doc.uploaded_by_user_id == admin.id
    assert doc.deleted_at is None

    # Verificar existencia física en disco
    expected_path = Path(document_service.config.documents_dir) / biz.id / f"{doc.id}.pdf"
    assert expected_path.is_file()
    assert expected_path.read_bytes() == VALID_PDF_BYTES


def test_save_valid_png_and_jpeg(test_setup):
    db, biz, admin = test_setup.db, test_setup.business, test_setup.admin

    doc_png = document_service.save_document(
        db=db,
        business=biz,
        doc_type=DOC_TYPE_CEDULA_REPRESENTANTE,
        description=None,
        filename="cedula.png",
        content=VALID_PNG_BYTES,
        uploaded_by=admin,
    )
    assert doc_png.content_type == "image/png"
    assert doc_png.storage_key.endswith(".png")

    doc_jpg = document_service.save_document(
        db=db,
        business=biz,
        doc_type=DOC_TYPE_CERTIFICACION_BANCARIA,
        description=None,
        filename="cert.jpg",
        content=VALID_JPEG_BYTES,
        uploaded_by=admin,
    )
    assert doc_jpg.content_type == "image/jpeg"
    assert doc_jpg.storage_key.endswith(".jpg")


def test_save_rejected_when_content_is_not_pdf_or_image(test_setup):
    db, biz, admin = test_setup.db, test_setup.business, test_setup.admin

    # Archivo .exe con nombre .pdf
    with pytest.raises(document_service.DocumentError) as exc_info:
        document_service.save_document(
            db=db,
            business=biz,
            doc_type=DOC_TYPE_RUT,
            description=None,
            filename="rut_falso.pdf",
            content=INVALID_EXE_BYTES,
            uploaded_by=admin,
        )
    assert exc_info.value.code == "INVALID_CONTENT_TYPE"


def test_save_rejected_when_size_exceeds_max_mb(test_setup, monkeypatch):
    db, biz, admin = test_setup.db, test_setup.business, test_setup.admin
    monkeypatch.setattr(document_service.config, "document_max_mb", 1)  # Límite 1 MB

    huge_content = VALID_PDF_BYTES + b"0" * (1024 * 1024 + 10)  # > 1 MB
    with pytest.raises(document_service.DocumentError) as exc_info:
        document_service.save_document(
            db=db,
            business=biz,
            doc_type=DOC_TYPE_RUT,
            description=None,
            filename="pesado.pdf",
            content=huge_content,
            uploaded_by=admin,
        )
    assert exc_info.value.code == "FILE_TOO_LARGE"


def test_save_rejected_when_doc_type_otro_without_description(test_setup):
    db, biz, admin = test_setup.db, test_setup.business, test_setup.admin

    with pytest.raises(document_service.DocumentError) as exc_info:
        document_service.save_document(
            db=db,
            business=biz,
            doc_type=DOC_TYPE_OTRO,
            description=None,
            filename="otro.pdf",
            content=VALID_PDF_BYTES,
            uploaded_by=admin,
        )
    assert exc_info.value.code == "DESCRIPTION_REQUIRED_FOR_OTHER"


def test_save_rejected_when_description_too_long(test_setup):
    db, biz, admin = test_setup.db, test_setup.business, test_setup.admin

    long_desc = "A" * 121
    with pytest.raises(document_service.DocumentError) as exc_info:
        document_service.save_document(
            db=db,
            business=biz,
            doc_type=DOC_TYPE_OTRO,
            description=long_desc,
            filename="otro.pdf",
            content=VALID_PDF_BYTES,
            uploaded_by=admin,
        )
    assert exc_info.value.code == "DESCRIPTION_TOO_LONG"


def test_save_rejected_when_doc_type_is_unknown(test_setup):
    db, biz, admin = test_setup.db, test_setup.business, test_setup.admin

    with pytest.raises(document_service.DocumentError) as exc_info:
        document_service.save_document(
            db=db,
            business=biz,
            doc_type="FACTURA_COMPRA",
            description=None,
            filename="factura.pdf",
            content=VALID_PDF_BYTES,
            uploaded_by=admin,
        )
    assert exc_info.value.code == "INVALID_DOC_TYPE"
    assert "RUT" in exc_info.value.message


def test_db_failure_cleans_up_orphaned_file(test_setup):
    db, biz, admin = test_setup.db, test_setup.business, test_setup.admin

    with patch.object(db, "commit", side_effect=RuntimeError("DB disk full")):
        with pytest.raises(RuntimeError):
            document_service.save_document(
                db=db,
                business=biz,
                doc_type=DOC_TYPE_RUT,
                description=None,
                filename="rut.pdf",
                content=VALID_PDF_BYTES,
                uploaded_by=admin,
            )

    # Verificar que no quedó ningún archivo huérfano en el directorio del negocio
    biz_dir = Path(document_service.config.documents_dir) / biz.id
    if biz_dir.exists():
        files = list(biz_dir.glob("*.pdf"))
        assert len(files) == 0


def test_open_file_security_and_existence(test_setup):
    db, biz, admin = test_setup.db, test_setup.business, test_setup.admin

    doc = document_service.save_document(
        db=db,
        business=biz,
        doc_type=DOC_TYPE_RUT,
        description=None,
        filename="rut.pdf",
        content=VALID_PDF_BYTES,
        uploaded_by=admin,
    )

    # Ruta legítima
    resolved = document_service.open_file(doc)
    assert resolved.is_file()
    assert resolved.read_bytes() == VALID_PDF_BYTES

    # Intento de Directory Traversal manipulando storage_key
    doc.storage_key = "../../etc/passwd"
    with pytest.raises(document_service.DocumentError) as exc_info:
        document_service.open_file(doc)
    assert exc_info.value.code == "INVALID_PATH"

    # Archivo eliminado de disco
    doc.storage_key = f"{biz.id}/non_existent_file.pdf"
    with pytest.raises(document_service.DocumentError) as exc_info:
        document_service.open_file(doc)
    assert exc_info.value.code == "FILE_NOT_FOUND"


def test_stable_numbering_and_retire(test_setup):
    db, biz, admin = test_setup.db, test_setup.business, test_setup.admin

    # Guardar 3 documentos en orden
    doc1 = document_service.save_document(
        db=db, business=biz, doc_type=DOC_TYPE_RUT, description=None,
        filename="doc1.pdf", content=VALID_PDF_BYTES, uploaded_by=admin,
    )
    doc2 = document_service.save_document(
        db=db, business=biz, doc_type=DOC_TYPE_CAMARA_COMERCIO, description=None,
        filename="doc2.pdf", content=VALID_PDF_BYTES, uploaded_by=admin,
    )
    doc3 = document_service.save_document(
        db=db, business=biz, doc_type=DOC_TYPE_OTRO, description="Contrato",
        filename="doc3.pdf", content=VALID_PDF_BYTES, uploaded_by=admin,
    )

    # Verificar numeración 1-based estable
    assert document_service.get_document_number(db, biz, doc1.id) == 1
    assert document_service.get_document_number(db, biz, doc2.id) == 2
    assert document_service.get_document_number(db, biz, doc3.id) == 3

    # Retirar documento #2
    retired = document_service.retire(db, biz, 2, by_user=admin)
    assert retired.id == doc2.id
    assert retired.deleted_at is not None
    assert retired.deleted_by_user_id == admin.id

    # Los números de doc1 y doc3 NO cambian tras retirar doc2 (numeración estable)
    assert document_service.get_document_number(db, biz, doc1.id) == 1
    assert document_service.get_document_number(db, biz, doc3.id) == 3

    # list_active solo devuelve los no retirados (doc3 y doc1, más reciente primero)
    active = document_service.list_active(db, biz)
    assert [d.id for d in active] == [doc3.id, doc1.id]

    # list_active_numbered devuelve pares (número, doc)
    active_numbered = document_service.list_active_numbered(db, biz)
    assert [(n, d.id) for n, d in active_numbered] == [(3, doc3.id), (1, doc1.id)]

    # Intentar retirar nuevamente el #2 -> ALREADY_RETIRED
    with pytest.raises(document_service.DocumentError) as exc_info:
        document_service.retire(db, biz, 2, by_user=admin)
    assert exc_info.value.code == "ALREADY_RETIRED"

    # Intentar retirar número inexistente -> DOCUMENT_NOT_FOUND
    with pytest.raises(document_service.DocumentError) as exc_info:
        document_service.retire(db, biz, 99, by_user=admin)
    assert exc_info.value.code == "DOCUMENT_NOT_FOUND"


def test_download_token_generation_and_verification(test_setup):
    db, biz, user, admin = test_setup.db, test_setup.business, test_setup.user, test_setup.admin

    doc = document_service.save_document(
        db=db, business=biz, doc_type=DOC_TYPE_RUT, description=None,
        filename="doc.pdf", content=VALID_PDF_BYTES, uploaded_by=admin,
    )

    token = document_service.create_download_token(user, doc)
    assert token

    # Verificación exitosa
    verified_doc, verified_user = document_service.verify_download_token(db, token)
    assert verified_doc.id == doc.id
    assert verified_user.id == user.id


def test_download_token_expired(test_setup):
    db, biz, user, admin = test_setup.db, test_setup.business, test_setup.user, test_setup.admin

    doc = document_service.save_document(
        db=db, business=biz, doc_type=DOC_TYPE_RUT, description=None,
        filename="doc.pdf", content=VALID_PDF_BYTES, uploaded_by=admin,
    )

    # Emitir token expirado (hace 10 minutos)
    secret = document_service.config.jwt_secret
    past_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    payload = {
        "purpose": "document_download",
        "sub": user.id,
        "did": doc.id,
        "iat": int((past_time - timedelta(minutes=5)).timestamp()),
        "exp": int(past_time.timestamp()),
    }
    expired_token = jwt.encode(payload, secret, algorithm="HS256")

    with pytest.raises(document_service.DocumentError) as exc_info:
        document_service.verify_download_token(db, expired_token)
    assert exc_info.value.code == "INVALID_TOKEN"
    assert exc_info.value.status_code == 401


def test_download_token_retired_document(test_setup):
    db, biz, user, admin = test_setup.db, test_setup.business, test_setup.user, test_setup.admin

    doc = document_service.save_document(
        db=db, business=biz, doc_type=DOC_TYPE_RUT, description=None,
        filename="doc.pdf", content=VALID_PDF_BYTES, uploaded_by=admin,
    )
    token = document_service.create_download_token(user, doc)

    # Retirar documento tras emitir el token
    document_service.retire(db, biz, 1, by_user=admin)

    with pytest.raises(document_service.DocumentError) as exc_info:
        document_service.verify_download_token(db, token)
    assert exc_info.value.code == "DOCUMENT_NOT_FOUND"
    assert exc_info.value.status_code == 404


def test_download_token_blocked_subscription(test_setup):
    db, biz, user, admin, sub = (
        test_setup.db, test_setup.business, test_setup.user, test_setup.admin, test_setup.sub,
    )

    doc = document_service.save_document(
        db=db, business=biz, doc_type=DOC_TYPE_RUT, description=None,
        filename="doc.pdf", content=VALID_PDF_BYTES, uploaded_by=admin,
    )
    token = document_service.create_download_token(user, doc)

    # Bloquear suscripción
    sub.status = "BLOQUEADO"
    db.commit()

    with pytest.raises(SubscriptionBlockedError):
        document_service.verify_download_token(db, token)
