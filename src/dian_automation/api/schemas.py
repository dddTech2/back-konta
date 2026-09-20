"""Esquemas Pydantic para los endpoints REST de Kontable."""

from datetime import datetime
from decimal import Decimal
from typing import List, Optional
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
    """Alerta de próximo vencimiento tributario."""
    dias: Optional[int] = None
    etiqueta: str
    limite: str
    estado: str = "proximo"


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
    limite: str
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


class OTPRequestResponse(BaseModel):
    detail: str = "Código enviado por Telegram"


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    """Estado de sesión para la SPA: negocio activo y visibilidad de funciones (sin datos fiscales)."""
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

