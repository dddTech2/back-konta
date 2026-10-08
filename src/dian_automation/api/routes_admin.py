"""Endpoints de administración web para Konta (Story 8.2, Story 8.3 y Story 8.4)."""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from dian_automation.api.dependencies import get_current_admin
from dian_automation.api.routes_config import _resolve_bot_username
from dian_automation.api.schemas import (
    AdminActivationLinkResponse,
    AdminClientCreateRequest,
    AdminClientCreateResponse,
    AdminClientsListResponse,
    AdminDocumentItem,
    AdminExtractionCreateRequest,
    AdminExtractionCreateResponse,
    AdminIncomeSourceRequest,
    AdminJobItem,
    AdminJobsListResponse,
    AdminPaymentCreateRequest,
    AdminPaymentItem,
    AdminPaymentResponse,
    AdminSummaryResponse,
    AdminTaxProfileRequest,
    AdminWorkerStatusResponse,
    ClientDetailResponse,
    DocumentLinkResponse,
)
from dian_automation.config import config
from dian_automation.core import admin_service, document_service
from dian_automation.core.admin_service import (
    INCOME_SOURCE_BY_TIPO,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    PLANS_CONFIG,
    AdminServiceError,
    NewClientData,
    normalize_nit,
)
from dian_automation.db.database import get_db
from dian_automation.db.models import Business, BusinessDocument, User
from dian_automation.telegram.notify import send_telegram_message

logger = logging.getLogger("routes_admin")

router = APIRouter(prefix="/api/admin", tags=["Admin"])


def _require_bot_username() -> str:
    """@username real del bot para el enlace t.me de activación.

    Sin él no se arma el enlace con un nombre supuesto: un nombre genérico puede ser el bot de un tercero
    y el cliente terminaría vinculándose con otro bot.
    """
    username = _resolve_bot_username()
    if not username:
        raise AdminServiceError(
            "BOT_USERNAME_UNAVAILABLE",
            "No pudimos consultar el nombre del bot de Telegram para armar el enlace. Intenta de nuevo en unos minutos.",
            status_code=503,
        )
    return username


@router.get("/ping")
def ping(admin: User = Depends(get_current_admin)) -> Dict[str, Any]:
    """Endpoint de salud protegido para verificar sesión y rol administrativo (Story 8.2)."""
    return {"ok": True}


@router.get("/clients", response_model=AdminClientsListResponse)
def list_clients(
    q: Optional[str] = Query(None, description="Término de búsqueda (contacto, razón social, comercial, NIT, celular)"),
    income_source: Optional[str] = Query(None, description="Filtrar por origen de ingresos (DIAN o MANUAL_SALES)"),
    status: Optional[str] = Query(None, description="Filtrar por estado de suscripción (ACTIVO, EN_MORA, BLOQUEADO, CANCELADO)"),
    page: int = Query(1, ge=1, description="Número de página"),
    page_size: int = Query(25, ge=1, le=100, description="Tamaño de página (máximo 100)"),
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Consulta paginada de clientes y negocios con filtros y búsqueda (Story 8.3 - AC #1)."""
    return admin_service.list_clients_paged(
        db=db,
        admin=admin,
        q=q,
        income_source=income_source,
        status=status,
        page=page,
        page_size=page_size,
    )


@router.get("/clients/{business_id}", response_model=ClientDetailResponse)
def get_client(
    business_id: str,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Ficha técnica y comercial completa de un negocio (Story 8.3 - AC #2)."""
    return admin_service.get_client_detail(db=db, business_id=business_id, admin=admin)


@router.post("/clients", response_model=AdminClientCreateResponse, status_code=status.HTTP_201_CREATED)
def create_client(
    body: AdminClientCreateRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Crea un nuevo cliente (persona o empresa) con negocio y suscripción (Story 8.3 - AC #3)."""
    tipo_clean = (body.person_type or "").upper().strip()
    if tipo_clean not in ("PERSONA", "EMPRESA"):
        raise AdminServiceError(
            "INVALID_CLIENT_TYPE",
            f"Tipo de cliente '{body.person_type}' no reconocido. Debe ser PERSONA o EMPRESA.",
            status_code=422,
        )

    full_name = (body.contact_name or "").strip()
    if not full_name:
        raise AdminServiceError("MISSING_NAME", "El nombre del cliente no puede estar vacío.", status_code=422)

    phone = (body.phone or "").strip()
    if not phone:
        raise AdminServiceError("MISSING_PHONE", "El teléfono no puede estar vacío.", status_code=422)

    raw_doc = body.nit if tipo_clean == "EMPRESA" else (body.document_number or body.nit)
    nit_clean = normalize_nit(raw_doc)
    if not nit_clean:
        campo = "NIT de la empresa" if tipo_clean == "EMPRESA" else "Cédula"
        raise AdminServiceError(
            "INVALID_NIT",
            f"El {campo} '{raw_doc}' no es válido (debe tener al menos 6 dígitos numéricos).",
            status_code=422,
        )

    company_name = None
    legal_rep_clean = None
    if tipo_clean == "EMPRESA":
        company_name = (body.company_name or "").strip()
        if not company_name:
            raise AdminServiceError("MISSING_BUSINESS_NAME", "El nombre de la empresa no puede estar vacío.", status_code=422)
        legal_rep_raw = (body.legal_rep_doc or "").strip()
        if not legal_rep_raw:
            raise AdminServiceError("MISSING_LEGAL_REP", "La Cédula del Representante Legal es obligatoria para empresas.", status_code=422)
        legal_rep_clean = "".join(filter(str.isdigit, legal_rep_raw))
        if len(legal_rep_clean) < 6:
            raise AdminServiceError(
                "INVALID_LEGAL_REP_DOC",
                f"La Cédula del Representante Legal '{body.legal_rep_doc}' no es válida (debe tener al menos 6 dígitos numéricos).",
                status_code=422,
            )

    plan_clean = (body.plan or "").upper().strip()
    if plan_clean not in PLANS_CONFIG:
        valid_plans = ", ".join(PLANS_CONFIG.keys())
        raise AdminServiceError(
            "INVALID_PLAN",
            f"Plan '{body.plan}' no reconocido. Opciones válidas: {valid_plans}",
            status_code=422,
        )

    income_source = None
    if body.income_source:
        inc_raw = body.income_source.upper().strip()
        if inc_raw in INCOME_SOURCE_BY_TIPO:
            income_source = INCOME_SOURCE_BY_TIPO[inc_raw]
        elif inc_raw in (INCOME_SOURCE_DIAN, INCOME_SOURCE_MANUAL_SALES):
            income_source = inc_raw
        else:
            raise AdminServiceError(
                "INVALID_INCOME_SOURCE",
                f"Módulo '{body.income_source}' no reconocido. Debe ser FACTURADOR o VENTAS_MANUALES.",
                status_code=422,
            )

    # Validar unicidad del NIT (AC #3: 409 si el NIT ya existe)
    existing_biz = db.query(Business).filter(Business.nit == nit_clean).first()
    if existing_biz:
        raise AdminServiceError("NIT_ALREADY_EXISTS", f"Ya existe un negocio registrado con el NIT {nit_clean}.", status_code=409)

    client_data = NewClientData(
        tipo_cliente=tipo_clean,
        full_name=full_name,
        phone=phone,
        business_name=company_name,
        nit=nit_clean,
        legal_rep_doc=legal_rep_clean,
        plan=plan_clean,
        income_source=income_source,
    )
    bot_username = _require_bot_username()
    try:
        created = admin_service.create_client(
            db=db,
            admin=admin,
            data=client_data,
            bot_username=bot_username,
            allow_existing_nit=False,
        )
    except AdminServiceError as e:
        status_code_mapped = 422 if e.status_code == 400 else e.status_code
        raise AdminServiceError(e.code, e.message, status_code_mapped)

    return AdminClientCreateResponse(
        business_id=created.business.id,
        user_id=created.user.id,
        activation_link=created.deep_link_url,
    )


@router.post("/clients/{business_id}/payments", response_model=AdminPaymentResponse, status_code=status.HTTP_201_CREATED)
def confirm_payment(
    business_id: str,
    body: AdminPaymentCreateRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Registra un pago comercial, reactiva la suscripción y notifica al cliente (Story 8.3 - AC #4)."""
    biz = db.query(Business).filter(
        (Business.id == business_id) | (Business.nit == business_id)
    ).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio con identificador '{business_id}'.", 404)

    result = admin_service.confirm_payment(
        db=db,
        admin=admin,
        nit=biz.nit,
        amount=body.amount,
        reference=body.reference,
        notifier=send_telegram_message,
    )

    payment_item = AdminPaymentItem(
        id=result.payment.id,
        amount=f"{result.payment.amount:.2f}",
        payment_date=result.payment.payment_date.isoformat(),
        reference_code=result.payment.reference_code,
        verified_by_admin_id=result.payment.verified_by_admin_id,
        created_at=result.payment.created_at.isoformat() if result.payment.created_at else None,
    )
    return AdminPaymentResponse(
        payment=payment_item,
        new_cutoff_date=result.new_cutoff_date.isoformat(),
        status=result.subscription.status,
        client_notified=result.client_notified,
        payment_id=result.payment.id,
        amount=f"{result.payment.amount:.2f}",
        reference=result.payment.reference_code,
    )


@router.patch("/clients/{business_id}/income-source", response_model=ClientDetailResponse)
def update_income_source(
    business_id: str,
    body: AdminIncomeSourceRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Modifica el origen de ingresos del negocio (DIAN o MANUAL_SALES) (Story 8.3 - AC #5)."""
    biz = db.query(Business).filter(
        (Business.id == business_id) | (Business.nit == business_id)
    ).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio con identificador '{business_id}'.", 404)

    admin_service.set_income_source(
        db=db,
        admin=admin,
        nit=biz.nit,
        income_source=body.income_source,
    )
    return admin_service.get_client_detail(db=db, business_id=biz.id, admin=admin)


@router.patch("/clients/{business_id}/tax-profile", response_model=ClientDetailResponse)
def update_tax_profile(
    business_id: str,
    body: AdminTaxProfileRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Actualiza el perfil tributario de un negocio facturador DIAN (Story 8.3 - AC #5)."""
    biz = db.query(Business).filter(
        (Business.id == business_id) | (Business.nit == business_id)
    ).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio con identificador '{business_id}'.", 404)

    admin_service.set_tax_profile(
        db=db,
        admin=admin,
        nit=biz.nit,
        iva_periodicity=body.iva_periodicity,
        is_withholding_agent=body.is_withholding_agent,
    )
    return admin_service.get_client_detail(db=db, business_id=biz.id, admin=admin)


@router.post("/clients/{business_id}/activation-link", response_model=AdminActivationLinkResponse)
def generate_activation_link(
    business_id: str,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Genera un nuevo enlace mágico de activación invalidando los anteriores no usados (Story 8.3 - AC #6)."""
    biz = db.query(Business).filter(
        (Business.id == business_id) | (Business.nit == business_id)
    ).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio con identificador '{business_id}'.", 404)

    user = biz.client or db.query(User).filter(User.id == biz.client_id).first()
    if not user:
        raise AdminServiceError("CLIENT_NOT_FOUND", "No se encontró el usuario cliente asociado al negocio.", 404)

    if user.is_telegram_linked or user.telegram_chat_id:
        raise AdminServiceError(
            "ALREADY_LINKED",
            "El cliente ya tiene su cuenta de Telegram vinculada.",
            status_code=409,
        )

    bot_username = _require_bot_username()
    link = admin_service.new_activation_link(
        db=db,
        admin=admin,
        user_id=user.id,
        bot_username=bot_username,
    )
    return AdminActivationLinkResponse(activation_link=link)


@router.post("/clients/{business_id}/release-telegram", response_model=ClientDetailResponse)
def release_telegram(
    business_id: str,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Desvincula la cuenta de Telegram del cliente dueño del negocio y responde la ficha (Story 8.3 - AC #7)."""
    admin_service.release_client_telegram(
        db=db,
        admin=admin,
        business_id=business_id,
    )
    return admin_service.get_client_detail(db=db, business_id=business_id, admin=admin)


# ==============================================================================
# Story 8.4: Resumen, Operación DIAN, Worker y Documentos
# ==============================================================================


@router.get("/summary", response_model=AdminSummaryResponse)
def get_admin_summary(
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Resumen consolidado de cartera, operaciones y estado del worker (Story 8.4 - AC #1)."""
    return admin_service.get_admin_summary(db=db, admin=admin)


@router.get("/jobs", response_model=AdminJobsListResponse)
def list_jobs(
    status: Optional[str] = Query(None, description="Filtrar por estado del trabajo de extracción"),
    business_id: Optional[str] = Query(None, description="Filtrar por ID o NIT del negocio"),
    page: int = Query(1, ge=1, description="Número de página"),
    page_size: int = Query(25, ge=1, le=100, description="Tamaño de página"),
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Listado paginado de trabajos de extracción DIAN más recientes primero (Story 8.4 - AC #2)."""
    return admin_service.list_jobs_paged(
        db=db,
        admin=admin,
        status=status,
        business_id=business_id,
        page=page,
        page_size=page_size,
    )


@router.get("/worker", response_model=AdminWorkerStatusResponse)
def get_worker_status(
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Estado y vigilancia del worker de descargas frente al umbral de silencio (Story 8.4 - AC #3)."""
    return admin_service.get_worker_status(db=db)


@router.post(
    "/clients/{business_id}/extractions",
    response_model=AdminExtractionCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_extraction(
    business_id: str,
    body: AdminExtractionCreateRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Encola un trabajo de extracción DIAN para el negocio especificado (Story 8.4 - AC #4)."""
    biz = db.query(Business).filter(
        (Business.id == business_id) | (Business.nit == business_id)
    ).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio con identificador '{business_id}'.", 404)

    period_spec = None
    if body.months is not None:
        if not (1 <= body.months <= 12):
            raise AdminServiceError("INVALID_MONTHS_RANGE", "El número de meses debe estar entre 1 y 12.", status_code=422)
        period_spec = admin_service.resolve_months_range(body.months)
    elif body.period is not None:
        p_clean = body.period.strip()
        if not re.match(r"^\d{4}-\d{2}$", p_clean):
            raise AdminServiceError("INVALID_PERIOD_FORMAT", f"Periodo '{body.period}' inválido. Usa formato YYYY-MM.", status_code=422)
        period_spec = p_clean

    job = admin_service.enqueue_extraction(
        db=db,
        admin=admin,
        nit=biz.nit,
        period_spec=period_spec,
    )

    return AdminExtractionCreateResponse(
        job_id=job.id,
        business_id=biz.id,
        target_period=job.target_period,
        status=job.status,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        next_run_at=job.next_run_at.isoformat() if job.next_run_at else None,
        created_at=job.created_at.isoformat() if job.created_at else None,
        id=job.id,
        period=job.target_period,
    )


@router.get("/clients/{business_id}/documents", response_model=List[AdminDocumentItem])
def list_client_documents(
    business_id: str,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Lista activa de documentos de un negocio con numeración estable (Story 8.4 - AC #5)."""
    biz = db.query(Business).filter(
        (Business.id == business_id) | (Business.nit == business_id)
    ).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio con identificador '{business_id}'.", 404)

    numbered = document_service.list_active_numbered(db, biz)
    items = []
    for num, doc in numbered:
        items.append(
            AdminDocumentItem(
                id=doc.id,
                number=num,
                doc_type=doc.doc_type,
                description=doc.description,
                original_filename=doc.original_filename,
                content_type=doc.content_type,
                size_bytes=doc.size_bytes,
                created_at=doc.created_at,
                document_id=doc.id,
            )
        )
    return items


@router.post(
    "/clients/{business_id}/documents",
    response_model=AdminDocumentItem,
    status_code=status.HTTP_201_CREATED,
)
async def upload_client_document(
    business_id: str,
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    description: Optional[str] = Form(None),
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Carga un documento para el cliente con validaciones y aviso por Telegram (Story 8.4 - AC #5 y #6)."""
    biz = db.query(Business).filter(
        (Business.id == business_id) | (Business.nit == business_id)
    ).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio con identificador '{business_id}'.", 404)

    clean_tipo = (doc_type or "").upper().strip()
    DOC_TYPE_ALIASES = {
        "RUT": "RUT",
        "CAMARA": "CAMARA_COMERCIO",
        "CAMARA_COMERCIO": "CAMARA_COMERCIO",
        "CEDULA": "CEDULA_REPRESENTANTE",
        "CEDULA_REPRESENTANTE": "CEDULA_REPRESENTANTE",
        "BANCARIA": "CERTIFICACION_BANCARIA",
        "CERTIFICACION_BANCARIA": "CERTIFICACION_BANCARIA",
        "OTRO": "OTRO",
    }
    if clean_tipo not in DOC_TYPE_ALIASES:
        valid_types = ", ".join(sorted(document_service.VALID_DOC_TYPES))
        raise AdminServiceError(
            "INVALID_DOC_TYPE",
            f"Tipo de documento '{doc_type}' no reconocido. Tipos válidos: {valid_types}.",
            status_code=422,
        )
    mapped_doc_type = DOC_TYPE_ALIASES[clean_tipo]

    clean_desc = description.strip() if description else None
    if mapped_doc_type == document_service.DOC_TYPE_OTRO and not clean_desc:
        raise AdminServiceError(
            "DESCRIPTION_REQUIRED_FOR_OTHER",
            "Los documentos de tipo 'OTRO' requieren una descripción.",
            status_code=422,
        )
    if clean_desc and len(clean_desc) > 120:
        raise AdminServiceError(
            "DESCRIPTION_TOO_LONG",
            "La descripción no puede superar 120 caracteres.",
            status_code=422,
        )

    # Lectura en bloques hasta DOCUMENT_MAX_MB + 1 para no cargar archivos gigantes a memoria (AC #6)
    max_mb = getattr(getattr(document_service, "config", None), "document_max_mb", getattr(config, "document_max_mb", 10))
    max_bytes = max_mb * 1024 * 1024
    chunks = []
    total_bytes = 0
    chunk_size = 65536
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise AdminServiceError(
                "FILE_TOO_LARGE",
                f"El archivo supera el tamaño máximo permitido de {max_mb} MB.",
                status_code=413,
            )
        chunks.append(chunk)

    content = b"".join(chunks)
    if len(content) == 0:
        raise AdminServiceError("EMPTY_FILE", "El archivo está vacío.", status_code=422)

    # Validar firma mágica
    try:
        content_type, ext = document_service.detect_content_type(content)
    except document_service.DocumentError as de:
        raise AdminServiceError(
            "INVALID_CONTENT_TYPE",
            de.message,
            status_code=415,
        )

    filename = file.filename or f"{mapped_doc_type}.{ext}"
    try:
        saved_doc = document_service.save_document(
            db=db,
            business=biz,
            doc_type=mapped_doc_type,
            description=clean_desc,
            filename=filename,
            content=content,
            uploaded_by=admin,
        )
    except document_service.DocumentError as de:
        mapped_status = 413 if de.code == "FILE_TOO_LARGE" else 422
        raise AdminServiceError(de.code, de.message, status_code=mapped_status)

    # Notificar al cliente si tiene Telegram vinculado (mismo texto que el bot admin)
    owner = biz.client or db.query(User).filter(User.id == biz.client_id).first()
    if owner and owner.is_telegram_linked and owner.telegram_chat_id:
        doc_label = document_service.DOC_TYPE_LABELS.get(saved_doc.doc_type, saved_doc.doc_type)
        client_msg = f"📄 Katerinn cargó tu {doc_label} en tu panel. Escribe /dashboard para verlo."
        try:
            send_telegram_message(owner.telegram_chat_id, client_msg)
        except Exception as e:
            logger.warning(f"Error enviando aviso de documento cargado a chat_id={owner.telegram_chat_id}: {e}")

    num = document_service.get_document_number(db, biz, saved_doc.id) or 1
    return AdminDocumentItem(
        id=saved_doc.id,
        number=num,
        doc_type=saved_doc.doc_type,
        description=saved_doc.description,
        original_filename=saved_doc.original_filename,
        content_type=saved_doc.content_type,
        size_bytes=saved_doc.size_bytes,
        created_at=saved_doc.created_at,
        document_id=saved_doc.id,
    )


@router.delete(
    "/clients/{business_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def retire_client_document(
    business_id: str,
    document_id: str,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Retira un documento de cliente con borrado lógico (Story 8.4 - AC #5)."""
    biz = db.query(Business).filter(
        (Business.id == business_id) | (Business.nit == business_id)
    ).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio con identificador '{business_id}'.", 404)

    # Buscar por UUID o por número estable
    doc = db.query(BusinessDocument).filter(
        BusinessDocument.id == document_id,
        BusinessDocument.business_id == biz.id,
    ).first()

    if not doc and document_id.isdigit():
        num = int(document_id)
        doc = document_service.find_document_by_number(db, biz, num)

    if not doc or doc.business_id != biz.id:
        raise AdminServiceError("DOCUMENT_NOT_FOUND", f"No se encontró el documento '{document_id}'.", 404)

    if doc.deleted_at is not None:
        raise AdminServiceError("ALREADY_RETIRED", "El documento ya fue retirado previamente.", status_code=409)

    doc.deleted_at = datetime.utcnow()
    doc.deleted_by_user_id = admin.id
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/clients/{business_id}/documents/{document_id}/link",
    response_model=DocumentLinkResponse,
)
def generate_client_document_link(
    business_id: str,
    document_id: str,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Genera enlace temporal firmado para que el administrador descargue el documento (Story 8.4 - AC #5)."""
    biz = db.query(Business).filter(
        (Business.id == business_id) | (Business.nit == business_id)
    ).first()
    if not biz:
        raise AdminServiceError("BUSINESS_NOT_FOUND", f"No se encontró ningún negocio con identificador '{business_id}'.", 404)

    doc = db.query(BusinessDocument).filter(
        BusinessDocument.id == document_id,
        BusinessDocument.business_id == biz.id,
        BusinessDocument.deleted_at.is_(None),
    ).first()

    if not doc and document_id.isdigit():
        num = int(document_id)
        candidate = document_service.find_document_by_number(db, biz, num)
        if candidate and candidate.deleted_at is None:
            doc = candidate

    if not doc:
        raise AdminServiceError("DOCUMENT_NOT_FOUND", "Documento no encontrado o no disponible.", 404)

    try:
        token = document_service.create_download_token(admin, doc)
    except document_service.DocumentError as de:
        raise AdminServiceError(de.code, de.message, de.status_code)

    return DocumentLinkResponse(
        url=f"/api/documents/file/{token}",
        expires_in=300,
    )

