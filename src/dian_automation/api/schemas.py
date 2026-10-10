"""Esquemas Pydantic para los endpoints REST de Konta."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class BusinessInfo(BaseModel):
    """Información del negocio activo."""
    id: str
    commercial_name: str
    legal_name: str
    nit: str
    dv: str
    economic_activity: Optional[str] = None
    taxpayer_type: str = "PERSONA_NATURAL"


class MetricSummary(BaseModel):
    """Resumen consolidado de facturación e impuestos del mes activo."""
    total: float
    ivaAcumulado: float
    numFacturas: int
    variacion: float
    periodo: str


class MonthlyBar(BaseModel):
    """Elemento del histórico de facturación para el gráfico de barras."""
    mes: str
    period_year_month: str
    total: float


class RecentInvoice(BaseModel):
    """Factura reciente mostrada en el Dashboard."""
    id: str
    num: str
    fecha: str
    cliente: str
    valor: float
    iva: float
    tipo: str = "Emitido"


class NextTaxAlert(BaseModel):
    """Alerta de próximo vencimiento tributario (viene del motor de calendario, Story 4.1b)."""
    dias: Optional[int] = None
    etiqueta: str
    limite: Optional[str] = None
    estado: str = "proximo"  # aldia, proximo, sin_datos (sin calendario cargado)
    tax_type: Optional[str] = None  # IVA_BIMESTRAL, RETEFUENTE, RENTA_*; None sin obligación


class SubscriptionInfo(BaseModel):
    """Estado de la suscripción y periodo de gracia."""
    estado: str
    plan: str
    has_warning_banner: bool = False
    days_left_in_grace: Optional[int] = None
    cutoff_date: Optional[str] = None
    grace_period_end: Optional[str] = None


class DashboardResponse(BaseModel):
    """Payload completo consumido por la pantalla Dashboard del prototipo."""
    business: BusinessInfo
    resumen: MetricSummary
    historico: List[MonthlyBar]
    facturasRecientes: List[RecentInvoice]
    alertaProximoVencimiento: NextTaxAlert
    suscripcion: SubscriptionInfo


class PeriodInvoiceItem(BaseModel):
    """Factura asociada a un periodo de IVA."""
    fecha: str
    cliente: str
    valor: float
    iva: float
    tipo: str


class IvaPeriodItem(BaseModel):
    """Periodo fiscal de IVA (bimestral o mensual) con desglose de ring SVG."""
    period_key: str
    etiqueta: str
    generado: float
    descontable: float
    saldo: float  # >0 Saldo a pagar, <0 Saldo a favor
    pct: float  # Proporción descontable/generado (0.0 a 1.0)
    estado: str  # en_curso, presentado
    limite: Optional[str] = None  # None sin obligación de IVA aplicable o sin calendario
    dias: Optional[int] = None
    facturas: List[PeriodInvoiceItem] = Field(default_factory=list)


class IvaDetailResponse(BaseModel):
    """Payload completo consumido por la pantalla de Detalle de IVA."""
    business_id: str
    nit: str
    nombre: str
    periodos: List[IvaPeriodItem]


class InvoiceDetailItem(BaseModel):
    """Factura detallada en el historial general."""
    id: str
    cufe: str
    num: str
    issue_date: str
    fecha_corta: str
    cliente: str
    nit_contraparte: str
    valor: float
    iva: float
    tipo_documento: str
    group_type: str  # Emitido, Recibido


class InvoicesListResponse(BaseModel):
    """Payload de historial de facturas con paginación."""
    total_count: int
    invoices: List[InvoiceDetailItem]
    limit: int
    offset: int


class LockoutErrorDetail(BaseModel):
    """Estructura de error 403 Forbidden cuando la cuenta está suspendida."""
    status_code: int = 403
    error: str = "SUBSCRIPTION_BLOCKED"
    message: str
    redirect_url: str = "/servicio-suspendido"
    plan: Optional[str] = None
    amount_due: Optional[float] = None


class OTPRequestSchema(BaseModel):
    """Solicitud de código OTP: teléfono o NIT del contribuyente."""
    identifier: str


class OTPVerifySchema(BaseModel):
    """Canje de un código OTP; `code` sin validar formato para responder 401 (no 422) si es inválido."""
    identifier: str
    code: str


class LinkLoginSchema(BaseModel):
    """Canje de un enlace firmado para acceso directo al dashboard (Story 7.2)."""
    token: str


class OTPRequestResponse(BaseModel):
    detail: str = "Código enviado por Telegram"


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    """Estado de sesión para la SPA: negocio activo y visibilidad de funciones (sin datos fiscales)."""
    role: str
    business_id: Optional[str] = None
    income_source: Optional[str] = None  # DIAN, MANUAL_SALES; null sin negocio (Story 6.1)
    is_provisioned: bool
    is_blocked: bool
    subscription_status: Optional[str] = None
    has_warning_banner: bool = False
    redirect_url: Optional[str] = None


class SaleCreateRequest(BaseModel):
    """Venta a registrar desde la web. Solo tipa los campos: la regla del total (> 0, 2 decimales) y el
    largo de la descripción son del servicio compartido `core/sales_service.py`."""
    total_amount: Decimal
    description: Optional[str] = None


class SaleResponse(BaseModel):
    """Venta registrada; `total_amount` viaja como Decimal (cadena en JSON), nunca como float."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    total_amount: Decimal
    description: Optional[str] = None
    recorded_via: str
    created_at: datetime


class SaleListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    total_amount: Decimal
    description: Optional[str] = None
    recorded_via: str
    sale_date: date
    created_at: datetime


class SaleListResponse(BaseModel):
    month: str            # 'YYYY-MM'
    sales: List[SaleListItem]


class SaleVoidResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    voided_at: datetime


class IncomeSummaryItem(BaseModel):
    """Resumen mensual de ingresos, egresos y utilidad (como cadenas decimales con 2 decimales)."""
    month: str
    ingresos: str
    egresos: str
    utilidad: str


class IncomeSummaryResponse(BaseModel):
    """Respuesta del endpoint de resumen de ingresos e historial de 6 meses."""
    month: str
    ingresos: str
    egresos: str
    utilidad: str
    historial: List[IncomeSummaryItem]



class CalendarObligation(BaseModel):
    """Obligación tributaria con fecha límite (Story 4.1b)."""
    tax_type: str
    etiqueta: str
    fecha_limite: str  # ISO YYYY-MM-DD
    estado: str  # completado, proximo, aldia
    dias: Optional[int] = None  # None si está completada


class CalendarResponse(BaseModel):
    """Respuesta de GET /api/calendar/{business_id}."""
    obligaciones: List[CalendarObligation]


class DocumentItem(BaseModel):
    """Elemento de documento de cliente para el panel web (Story 7.4a)."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    doc_type: str
    description: Optional[str] = None
    original_filename: str
    content_type: str
    size_bytes: int
    created_at: datetime


class DocumentListResponse(BaseModel):
    """Respuesta de GET /api/documents/{business_id}."""
    documents: List[DocumentItem]


class DocumentLinkResponse(BaseModel):
    """Respuesta de POST /api/documents/{business_id}/{document_id}/link."""
    url: str
    expires_in: int = 300


# ==============================================================================
# Story 8.3: Esquemas de Administración de Clientes, Pagos y Configuración
# ==============================================================================


class AdminClientListItem(BaseModel):
    """Ítem de cliente en la lista paginada del panel de administración (AC #1)."""
    business_id: str
    user_id: str
    legal_name: str
    commercial_name: str
    nit: str
    dv: str
    income_source: str
    taxpayer_type: str
    contact_name: str
    phone: Optional[str] = None
    is_telegram_linked: bool
    plan: Optional[str] = None
    subscription_status: Optional[str] = None
    cutoff_date: Optional[str] = None
    grace_period_end: Optional[str] = None
    is_active: bool


class AdminClientsListResponse(BaseModel):
    """Respuesta paginada del listado de clientes (AC #1)."""
    items: List[AdminClientListItem]
    total: int
    page: int
    page_size: int


class AdminClientCreateRequest(BaseModel):
    """Alta de cliente desde el panel web de administración (AC #3)."""
    person_type: str  # "PERSONA" o "EMPRESA"
    contact_name: str
    phone: str
    document_number: Optional[str] = None
    company_name: Optional[str] = None
    nit: Optional[str] = None
    legal_rep_doc: Optional[str] = None
    plan: str
    income_source: Optional[str] = None


class AdminClientCreateResponse(BaseModel):
    """Resultado del alta de cliente (AC #3, Story 9.2 AC #9)."""
    business_id: str
    user_id: str
    activation_link: str
    final_price: Optional[str] = None
    monthly_price: Optional[str] = None
    discount_rate: Optional[str] = None
    cutoff_date: Optional[str] = None


class AdminPaymentCreateRequest(BaseModel):
    """Registro de pago comercial (AC #4, Story 9.1, Story 9.2 AC #10)."""
    amount: Any
    reference: str
    allow_mismatch: bool = False


class AdminPaymentItem(BaseModel):
    """Detalle de un pago registrado (AC #4)."""
    id: str
    amount: str
    payment_date: str
    reference_code: Optional[str] = None
    verified_by_admin_id: Optional[str] = None
    created_at: Optional[str] = None


class AdminPaymentResponse(BaseModel):
    """Respuesta al confirmar pago (AC #4, Story 9.1, Story 9.2 AC #10)."""
    payment: AdminPaymentItem
    new_cutoff_date: str
    status: str
    client_notified: bool
    payment_id: Optional[str] = None
    amount: Optional[str] = None
    reference: Optional[str] = None
    expected_amount: Optional[Any] = None
    difference: Optional[Any] = None


class AdminIncomeSourceRequest(BaseModel):
    """Actualización del origen de ingresos (AC #5)."""
    income_source: str


class AdminTaxProfileRequest(BaseModel):
    """Actualización del perfil tributario (AC #5)."""
    iva_periodicity: Optional[str] = None
    is_withholding_agent: bool = False


class AdminActivationLinkResponse(BaseModel):
    """Nuevo enlace de activación (AC #6)."""
    activation_link: str


class AdminClientBusiness(BaseModel):
    """Datos del negocio en la ficha del cliente."""
    id: str
    legal_name: str
    commercial_name: str
    nit: str
    dv: str
    taxpayer_type: str
    legal_rep_doc: Optional[str] = None
    economic_activity: Optional[str] = None
    income_source: str
    is_active: bool
    created_at: Optional[str] = None


class AdminClientContact(BaseModel):
    """Datos del contacto en la ficha del cliente."""
    id: str
    full_name: str
    phone: Optional[str] = None
    email: str
    is_telegram_linked: bool
    telegram_chat_id: Optional[int] = None
    telegram_username: Optional[str] = None


class AdminTaxProfile(BaseModel):
    """Perfil tributario en la ficha del cliente."""
    iva_periodicity: Optional[str] = None
    is_withholding_agent: bool = False


class SubscriptionPriceChangeItem(BaseModel):
    id: str
    created_at: Optional[str] = None
    old_plan: Optional[str] = None
    new_plan: Optional[str] = None
    old_monthly_price: Optional[str] = None
    new_monthly_price: Optional[str] = None
    old_discount_rate: Optional[str] = None
    new_discount_rate: Optional[str] = None
    old_final_price: Optional[str] = None
    new_final_price: Optional[str] = None
    reason: str
    admin: Optional[str] = None


class AdminSubscriptionDetail(BaseModel):
    """Suscripción vigente en la ficha del cliente (Story 9.2 AC #7)."""
    id: str
    plan: str
    status: str
    discount_rate: Optional[str] = None
    base_price: Optional[str] = None
    final_price: Optional[str] = None
    monthly_price: Optional[str] = None
    price_origin: Optional[str] = "TARIFA"
    price_note: Optional[str] = None
    current_rate_monthly: Optional[str] = None
    price_changes: List[SubscriptionPriceChangeItem] = Field(default_factory=list)
    start_date: Optional[str] = None
    cutoff_date: Optional[str] = None
    grace_period_end: Optional[str] = None


class AdminPaymentSummary(BaseModel):
    """Pago resumido en la ficha del cliente (Story 9.2 AC #10)."""
    id: str
    payment_date: Optional[str] = None
    amount: str
    expected_amount: Optional[str] = None
    reference_code: Optional[str] = None
    payment_method: Optional[str] = "TRANSFERENCIA"
    verified_by_admin_id: Optional[str] = None
    created_at: Optional[str] = None


class AdminExtractionSummary(BaseModel):
    """Extracción DIAN en la ficha del cliente."""
    id: str
    period: str
    status: str
    attempts: int
    next_run_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_code: Optional[str] = None


class ClientDetailResponse(BaseModel):
    """Ficha completa del cliente (AC #2, #5, #7)."""
    business: AdminClientBusiness
    contact: AdminClientContact
    tax_profile: AdminTaxProfile
    subscription: Optional[AdminSubscriptionDetail] = None
    recent_payments: List[AdminPaymentSummary] = Field(default_factory=list)
    recent_extractions: List[AdminExtractionSummary] = Field(default_factory=list)
    active_documents_count: int = 0
    has_pending_activation_link: bool = False

    # Campos top-level de conveniencia
    business_id: Optional[str] = None
    legal_name: Optional[str] = None
    commercial_name: Optional[str] = None
    nit: Optional[str] = None
    dv: Optional[str] = None
    income_source: Optional[str] = None
    taxpayer_type: Optional[str] = None
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    is_telegram_linked: Optional[bool] = None
    telegram_chat_id: Optional[int] = None
    iva_periodicity: Optional[str] = None
    is_withholding_agent: Optional[bool] = None


# ==============================================================================
# Story 8.4: Esquemas de Operación de Administración (Resumen, Jobs, Worker, Docs)
# ==============================================================================


class AdminSummaryCutoffItem(BaseModel):
    """Negocio con corte próximo en los siguientes 7 días (AC #1)."""
    business_id: str
    commercial_name: str
    nit: str
    cutoff_date: str


class AdminSummaryGraceItem(BaseModel):
    """Negocio en periodo de gracia (AC #1)."""
    business_id: str
    commercial_name: str
    nit: str
    cutoff_date: Optional[str] = None
    grace_period_end: Optional[str] = None


class AdminSummaryPaymentsMonth(BaseModel):
    """Recaudo del mes en curso (AC #1)."""
    count: int
    total: float


class AdminWorkerItem(BaseModel):
    """Estado y latido de un worker individual (AC #3)."""
    name: str
    last_seen_at: Optional[str] = None
    minutes_since: Optional[int] = None
    is_silent: bool


class AdminWorkerStatusResponse(BaseModel):
    """Vigilancia y estado de silencio de workers (AC #3)."""
    silence_threshold_minutes: int
    workers: List[AdminWorkerItem]


class AdminSummaryResponse(BaseModel):
    """Resumen consolidado de administración web (AC #1)."""
    clients_by_status: Dict[str, int]
    clients_by_income_source: Dict[str, int]
    upcoming_cutoffs: List[AdminSummaryCutoffItem]
    in_grace: List[AdminSummaryGraceItem]
    payments_this_month: AdminSummaryPaymentsMonth
    failed_jobs_24h: int
    unlinked_telegram: int
    worker: AdminWorkerStatusResponse


class AdminJobItem(BaseModel):
    """Trabajo de extracción DIAN en el listado de administración (AC #2)."""
    job_id: str
    business_id: str
    commercial_name: str
    nit: str
    target_period: str
    status: str
    attempt_count: int
    max_attempts: int
    next_run_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class AdminJobsListResponse(BaseModel):
    """Respuesta paginada del listado de extracciones (AC #2)."""
    items: List[AdminJobItem]
    total: int
    page: int
    page_size: int


class AdminExtractionCreateRequest(BaseModel):
    """Solicitud para encolar extracción DIAN (AC #4)."""
    period: Optional[str] = None
    months: Optional[int] = None


class AdminExtractionCreateResponse(BaseModel):
    """Respuesta al encolar extracción DIAN (AC #4)."""
    job_id: str
    business_id: str
    target_period: str
    status: str
    attempt_count: int = 0
    max_attempts: int = 3
    next_run_at: Optional[str] = None
    created_at: Optional[str] = None
    id: Optional[str] = None
    period: Optional[str] = None


class AdminDocumentItem(BaseModel):
    """Elemento de documento de cliente para el panel de administración (AC #5)."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    number: int
    doc_type: str
    description: Optional[str] = None
    original_filename: str
    content_type: str
    size_bytes: int
    created_at: datetime
    document_id: Optional[str] = None


# ==============================================================================
# Story 9.2: Esquemas de Administración de Tarifas, Precios y Planes
# ==============================================================================


class PricingPlanItem(BaseModel):
    code: str
    name: str
    months: int
    discount_rate: str
    is_active: bool
    sort_order: int


class PricingPlanCreateRequest(BaseModel):
    code: str
    name: str
    months: int
    discount_rate: Any


class PricingPlanUpdateRequest(BaseModel):
    name: Optional[str] = None
    discount_rate: Optional[Any] = None
    is_active: Optional[bool] = None
    months: Optional[Any] = None


class PricingRateItem(BaseModel):
    income_source: str
    taxpayer_type: str
    monthly_price: str
    effective_from: str


class PricingScheduledRateItem(BaseModel):
    id: str
    income_source: str
    taxpayer_type: str
    monthly_price: str
    effective_from: str


class PricingQuoteItem(BaseModel):
    income_source: str
    taxpayer_type: str
    plan: str
    months: int
    monthly_price: str
    gross_total: str
    discount_rate: str
    discount_amount: str
    final_price: str


class PricingOverviewResponse(BaseModel):
    """Respuesta de GET /api/admin/pricing (AC #1)."""
    grace_days: int
    plans: List[PricingPlanItem]
    rates: List[PricingRateItem]
    scheduled: List[PricingScheduledRateItem]
    quotes: List[PricingQuoteItem]


class BillingSettingsResponse(BaseModel):
    """Configuración de días de gracia (AC #2c)."""
    grace_days: int
    updated_at: Optional[str] = None


class BillingSettingsUpdateRequest(BaseModel):
    """Actualización de días de gracia (AC #2c)."""
    grace_days: int


class PricingRateCreateRequest(BaseModel):
    """Creación o actualización de tarifa (AC #3)."""
    income_source: str
    taxpayer_type: str
    monthly_price: Any
    effective_from: Optional[date] = None


class PricingRateResponse(BaseModel):
    """Respuesta de tarifa creada/actualizada (AC #3)."""
    id: str
    income_source: str
    taxpayer_type: str
    monthly_price: str
    effective_from: str
    created_by: Optional[str] = None


class PricingRateHistoryItem(BaseModel):
    """Elemento del histórico de tarifas (AC #5)."""
    id: str
    income_source: str
    taxpayer_type: str
    monthly_price: str
    effective_from: str
    created_at: Optional[str] = None
    created_by: Optional[str] = None


class PricingQuoteDetailResponse(PricingQuoteItem):
    """Cotización individual con fechas calculadas (AC #6)."""
    start_date: str
    cutoff_date: str


class ClientSubscriptionUpdateRequest(BaseModel):
    """Modificación de la suscripción de un cliente (AC #8)."""
    plan: Optional[str] = None
    monthly_price: Optional[Any] = None
    discount_rate: Optional[Any] = None
    use_current_rate: Optional[bool] = None
    reason: str



