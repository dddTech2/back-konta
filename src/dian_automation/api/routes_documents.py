"""Rutas de la API para gestión y descarga de documentos de clientes (Story 7.4a)."""

from urllib.parse import quote
from typing import Any, Dict, Tuple

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from dian_automation.api.dependencies import get_business_with_access
from dian_automation.api.schemas import (
    DocumentItem,
    DocumentLinkResponse,
    DocumentListResponse,
)
from dian_automation.core import document_service
from dian_automation.db.database import get_db
from dian_automation.db.models import Business, BusinessDocument, User

router = APIRouter(prefix="/api/documents", tags=["Documentos"])


@router.get("/{business_id}", response_model=DocumentListResponse)
def list_documents(
    business_id: str,
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    db: Session = Depends(get_db),
) -> DocumentListResponse:
    """Lista los documentos activos del negocio, ordenados de más reciente a más antiguo.

    Requiere sesión activa y pertenencia del negocio. Aplica a negocios DIAN y MANUAL_SALES.
    Nunca expone storage_key ni sha256.
    """
    business, _user, _access = business_access
    docs = document_service.list_active(db, business)
    return DocumentListResponse(
        documents=[DocumentItem.model_validate(doc) for doc in docs]
    )


@router.post("/{business_id}/{document_id}/link", response_model=DocumentLinkResponse)
def generate_document_link(
    business_id: str,
    document_id: str,
    business_access: Tuple[Business, User, Dict[str, Any]] = Depends(get_business_with_access),
    db: Session = Depends(get_db),
) -> DocumentLinkResponse:
    """Genera un enlace temporal (5 min) firmado con JWT para descargar el documento.

    Documento inexistente, ajeno o retirado -> 404.
    """
    business, user, _access = business_access
    doc = (
        db.query(BusinessDocument)
        .filter(
            BusinessDocument.id == document_id,
            BusinessDocument.business_id == business.id,
            BusinessDocument.deleted_at.is_(None),
        )
        .first()
    )
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Documento no encontrado o no disponible.",
        )

    try:
        token = document_service.create_download_token(user, doc)
    except document_service.DocumentError as de:
        raise HTTPException(status_code=de.status_code, detail=de.message)

    return DocumentLinkResponse(
        url=f"/api/documents/file/{token}",
        expires_in=300,
    )


@router.get("/file/{token}")
def download_document_file(
    token: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    """Descarga directa de un documento mediante token JWT temporal sin cabecera Authorization.

    Valida firma, expiración, purpose, usuario activo, propiedad del negocio y estado
    de la suscripción (403 si está bloqueada). El token no se registra en logs.
    """
    try:
        doc, _user = document_service.verify_download_token(db, token)
    except document_service.DocumentError as de:
        raise HTTPException(status_code=de.status_code, detail=de.message)

    try:
        file_path = document_service.open_file(doc)
    except document_service.DocumentError as de:
        raise HTTPException(status_code=de.status_code, detail=de.message)

    ext = doc.storage_key.rsplit(".", 1)[-1] if "." in doc.storage_key else "bin"
    ascii_fallback = f"documento.{ext}"
    quoted_filename = quote(doc.original_filename)
    disposition = f'inline; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quoted_filename}'

    headers = {
        "Content-Disposition": disposition,
        "Cache-Control": "private, no-store",
    }
    return FileResponse(
        path=file_path,
        media_type=doc.content_type,
        headers=headers,
    )
