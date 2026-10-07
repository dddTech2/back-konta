"""Endpoints de administración web para Konta (Story 8.2 y Story 8.3)."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from dian_automation.api.dependencies import get_current_admin
from dian_automation.api.routes_config import _resolve_bot_username
from dian_automation.api.schemas import (
    AdminActivationLinkResponse,
    AdminClientCreateRequest,
    AdminClientCreateResponse,
    AdminClientsListResponse,
    AdminIncomeSourceRequest,
    AdminPaymentCreateRequest,
    AdminPaymentItem,
    AdminPaymentResponse,
    AdminTaxProfileRequest,
    ClientDetailResponse,
)
from dian_automation.core import admin_service
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
from dian_automation.db.models import Business, User
from dian_automation.telegram.notify import send_telegram_message

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
