"""Servicio centralizado de gestión de documentos de clientes (Story 7.4a).

Único lugar con la lógica de validación, almacenamiento en disco, numeración estable,
retiro y generación de enlaces de descarga segura para documentos de clientes (RUT,
Cámara de Comercio, Cédula, Certificación Bancaria y Otros).
"""

import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jwt
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from dian_automation.config import config
from dian_automation.db.models import (
    Business,
    BusinessDocument,
    User,
    DOC_TYPE_RUT,
    DOC_TYPE_CAMARA_COMERCIO,
    DOC_TYPE_CEDULA_REPRESENTANTE,
    DOC_TYPE_CERTIFICACION_BANCARIA,
    DOC_TYPE_OTRO,
    DOCUMENT_TYPES,
)
from dian_automation.subscriptions.lockout_service import (
    SubscriptionBlockedError,
    SubscriptionLockoutService,
)

logger = logging.getLogger("document_service")

# Tipos válidos y etiquetas legibles para el usuario
VALID_DOC_TYPES = set(DOCUMENT_TYPES)

DOC_TYPE_LABELS: Dict[str, str] = {
    DOC_TYPE_RUT: "RUT",
    DOC_TYPE_CAMARA_COMERCIO: "Cámara de comercio",
    DOC_TYPE_CEDULA_REPRESENTANTE: "Cédula del representante",
    DOC_TYPE_CERTIFICACION_BANCARIA: "Certificación bancaria",
    DOC_TYPE_OTRO: "Otro documento",
}

# Firmas mágicas de archivos permitidos (bytes iniciales)
MAGIC_PDF = b"%PDF-"
MAGIC_PNG = b"\x89PNG\r\n\x1a\n"
MAGIC_JPEG = b"\xff\xd8\xff"


class DocumentError(Exception):
    """Excepción de dominio para operaciones de documentos con mensajes legibles."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _secret() -> str:
    """Obtiene el secreto JWT de la configuración; si falta, lanza error 500."""
    if not config.jwt_secret:
        logger.error("JWT_SECRET no configurado: no se pueden emitir ni verificar enlaces de descarga.")
        raise DocumentError(
            "AUTH_CONFIG_ERROR",
            "Autenticación no configurada en el servidor.",
            status_code=500,
        )
    return config.jwt_secret


def detect_content_type(content: bytes) -> Tuple[str, str]:
    """Detecta el tipo MIME y la extensión a partir de la firma de bytes iniciales.

    Lanza DocumentError si el contenido no corresponde a PDF, PNG o JPEG.
    """
    if content.startswith(MAGIC_PDF):
        return "application/pdf", "pdf"
    elif content.startswith(MAGIC_PNG):
        return "image/png", "png"
    elif content.startswith(MAGIC_JPEG):
        return "image/jpeg", "jpg"
    else:
        raise DocumentError(
            "INVALID_CONTENT_TYPE",
            "Tipo de archivo no permitido. Solo se aceptan documentos PDF e imágenes PNG o JPEG.",
            status_code=400,
        )


def save_document(
    db: Session,
    business: Business,
    doc_type: str,
    description: Optional[str],
    filename: str,
    content: bytes,
    uploaded_by: User,
) -> BusinessDocument:
    """Valida, almacena en disco y registra en base de datos un nuevo documento de cliente.

    - Tamaño <= DOCUMENT_MAX_MB
    - Tipo detectado por contenido (magic bytes)
    - doc_type conocido
    - OTRO exige descripción; descripción <= 120 caracteres
    - Archivo guardado atómicamente en <DOCUMENTS_DIR>/<business_id>/<document_id>.<ext>
    - Si falla la base de datos, el archivo en disco se elimina
    """
    max_bytes = config.document_max_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise DocumentError(
            "FILE_TOO_LARGE",
            f"El archivo supera el tamaño máximo permitido de {config.document_max_mb} MB.",
            status_code=400,
        )

    if doc_type not in VALID_DOC_TYPES:
        valid_str = ", ".join(sorted(VALID_DOC_TYPES))
        raise DocumentError(
            "INVALID_DOC_TYPE",
            f"Tipo de documento desconocido: '{doc_type}'. Tipos válidos: {valid_str}.",
            status_code=400,
        )

    clean_desc = description.strip() if description else None
    if doc_type == DOC_TYPE_OTRO and not clean_desc:
        raise DocumentError(
            "DESCRIPTION_REQUIRED_FOR_OTHER",
            "Los documentos de tipo 'OTRO' requieren una descripción.",
            status_code=400,
        )

    if clean_desc and len(clean_desc) > 120:
        raise DocumentError(
            "DESCRIPTION_TOO_LONG",
            "La descripción no puede superar 120 caracteres.",
            status_code=400,
        )

    content_type, ext = detect_content_type(content)
    sha256 = hashlib.sha256(content).hexdigest()
    doc_id = str(uuid.uuid4())

    base_dir = Path(config.documents_dir).resolve()
    biz_dir = base_dir / str(business.id)
    biz_dir.mkdir(parents=True, exist_ok=True)

    final_filename = f"{doc_id}.{ext}"
    final_path = biz_dir / final_filename
    temp_path = biz_dir / f".tmp_{doc_id}_{uuid.uuid4().hex}"

    # Guardar en temporal y renombrar atómicamente
    temp_path.write_bytes(content)
    os.replace(temp_path, final_path)

    storage_key = f"{business.id}/{final_filename}"
    safe_original_name = os.path.basename(filename)[:255] if filename else f"{doc_type}.{ext}"

    document = BusinessDocument(
        id=doc_id,
        business_id=business.id,
        doc_type=doc_type,
        description=clean_desc,
        original_filename=safe_original_name,
        content_type=content_type,
        size_bytes=len(content),
        storage_key=storage_key,
        sha256=sha256,
        uploaded_by_user_id=uploaded_by.id,
        created_at=datetime.utcnow(),
    )

    try:
        db.add(document)
        db.commit()
        db.refresh(document)
    except Exception as e:
        db.rollback()
        if final_path.exists():
            try:
                final_path.unlink()
            except OSError:
                pass
        logger.error(f"Error persistiendo documento en base de datos: {e}", exc_info=True)
        raise

    logger.info(
        f"Documento guardado: id={document.id}, negocio={business.nit}, tipo={document.doc_type}, "
        f"tamaño={document.size_bytes} bytes"
    )
    return document


def list_active(db: Session, business: Business) -> List[BusinessDocument]:
    """Lista todos los documentos activos del negocio, ordenados por created_at desc, id desc."""
    return (
        db.query(BusinessDocument)
        .filter(
            BusinessDocument.business_id == business.id,
            BusinessDocument.deleted_at.is_(None),
        )
        .order_by(BusinessDocument.created_at.desc(), BusinessDocument.id.desc())
        .all()
    )


def _all_documents_ordered(db: Session, business: Business) -> List[BusinessDocument]:
    """Todos los documentos del negocio (activos y retirados) ordenados por (created_at asc, id asc)."""
    return (
        db.query(BusinessDocument)
        .filter(BusinessDocument.business_id == business.id)
        .order_by(BusinessDocument.created_at.asc(), BusinessDocument.id.asc())
        .all()
    )


def get_document_number(db: Session, business: Business, document_id: str) -> Optional[int]:
    """Devuelve la posición 1-based estable de un documento en el negocio."""
    all_docs = _all_documents_ordered(db, business)
    for idx, doc in enumerate(all_docs, start=1):
        if doc.id == document_id:
            return idx
    return None


def list_active_numbered(db: Session, business: Business) -> List[Tuple[int, BusinessDocument]]:
    """Lista los documentos activos del negocio, cada uno con su número estable 1-based.

    El número es la posición entre TODOS los documentos del negocio ordenados por (created_at, id).
    Se devuelven ordenados de más reciente a más antiguo (created_at desc).
    """
    all_docs = _all_documents_ordered(db, business)
    numbered = [(idx, doc) for idx, doc in enumerate(all_docs, start=1) if doc.deleted_at is None]
    numbered.sort(key=lambda item: (item[1].created_at, item[1].id), reverse=True)
    return numbered


def find_document_by_number(db: Session, business: Business, number: int) -> Optional[BusinessDocument]:
    """Busca un documento por su posición 1-based estable entre todos los documentos del negocio."""
    if number <= 0:
        return None
    all_docs = _all_documents_ordered(db, business)
    if 1 <= number <= len(all_docs):
        return all_docs[number - 1]
    return None


def retire(db: Session, business: Business, number: int, by_user: User) -> BusinessDocument:
    """Retira un documento según su número estable en el negocio.

    Marca deleted_at y deleted_by_user_id. Lanza DocumentError si el número no existe o ya está retirado.
    """
    doc = find_document_by_number(db, business, number)
    if doc is None:
        raise DocumentError(
            "DOCUMENT_NOT_FOUND",
            f"No se encontró ningún documento con el número {number}.",
            status_code=404,
        )

    if doc.deleted_at is not None:
        raise DocumentError(
            "ALREADY_RETIRED",
            f"El documento #{number} ya fue retirado previamente.",
            status_code=400,
        )

    doc.deleted_at = datetime.utcnow()
    doc.deleted_by_user_id = by_user.id
    try:
        db.commit()
        db.refresh(doc)
    except Exception as e:
        db.rollback()
        logger.error(f"Error retirando documento #{number}: {e}", exc_info=True)
        raise

    logger.info(f"Documento retirado: id={doc.id}, numero={number}, por_usuario={by_user.id}")
    return doc


def open_file(document: BusinessDocument) -> Path:
    """Verifica que el archivo existe y que su ruta no escapa de DOCUMENTS_DIR.

    Devuelve el Path al archivo físico o lanza DocumentError.
    """
    base_dir = Path(config.documents_dir).resolve()
    file_path = (base_dir / document.storage_key).resolve()

    try:
        file_path.relative_to(base_dir)
    except ValueError:
        raise DocumentError(
            "INVALID_PATH",
            "Ruta de archivo inválida o no permitida.",
            status_code=400,
        )

    if not file_path.is_file():
        raise DocumentError(
            "FILE_NOT_FOUND",
            "El archivo no existe en el almacenamiento.",
            status_code=404,
        )

    return file_path


def create_download_token(user: User, document: BusinessDocument) -> str:
    """Genera un token JWT temporal (5 minutos) para descargar un documento sin sesión HTTP.

    Payload: purpose="document_download", sub=user.id, did=document.id, exp=5 min.
    """
    secret = _secret()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=5)
    payload = {
        "purpose": "document_download",
        "sub": user.id,
        "did": document.id,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=config.jwt_algorithm or "HS256")


def verify_download_token(db: Session, token: str) -> Tuple[BusinessDocument, User]:
    """Valida el token JWT de descarga de documento.

    Verifica:
    - Firma y vigencia (exp)
    - purpose == 'document_download'
    - Usuario activo
    - Usuario dueño del negocio asociado al documento
    - Documento existente y no retirado (deleted_at is None)
    - Suscripción no bloqueada (SubscriptionBlockedError -> 403)

    Lanza DocumentError (401 o 404) o SubscriptionBlockedError.
    NO escribe el token en logs.
    """
    secret = _secret()
    try:
        payload = jwt.decode(token, secret, algorithms=[config.jwt_algorithm or "HS256"])
    except (jwt.PyJWTError, Exception):
        raise DocumentError(
            "INVALID_TOKEN",
            "Enlace de descarga inválido o expirado.",
            status_code=401,
        )

    if payload.get("purpose") != "document_download":
        raise DocumentError(
            "INVALID_TOKEN",
            "Enlace de descarga inválido o expirado.",
            status_code=401,
        )

    sub = payload.get("sub")
    did = payload.get("did")
    if not sub or not did:
        raise DocumentError(
            "INVALID_TOKEN",
            "Enlace de descarga inválido o expirado.",
            status_code=401,
        )

    user = db.query(User).filter(User.id == sub, User.is_active.is_(True)).first()
    if not user:
        raise DocumentError(
            "INVALID_TOKEN",
            "Enlace de descarga inválido o expirado.",
            status_code=401,
        )

    document = db.query(BusinessDocument).filter(BusinessDocument.id == did).first()
    if not document or document.deleted_at is not None:
        raise DocumentError(
            "DOCUMENT_NOT_FOUND",
            "Documento no encontrado o no disponible.",
            status_code=404,
        )

    # Validar que el usuario sea el dueño del negocio del documento
    business = (
        db.query(Business)
        .filter(Business.id == document.business_id, Business.client_id == user.id)
        .first()
    )
    if not business:
        raise DocumentError(
            "DOCUMENT_NOT_FOUND",
            "Documento no encontrado o no disponible.",
            status_code=404,
        )

    # Validar estado de la suscripción
    access = SubscriptionLockoutService.verify_user_web_access(user.id, db)
    if not access["allowed"]:
        raise SubscriptionBlockedError(
            message=access["message"],
            status_code=access["status_code"],
            redirect_url=access.get("redirect_url", "/servicio-suspendido"),
            error_code=access.get("error_code", "SUBSCRIPTION_BLOCKED"),
        )

    return document, user
