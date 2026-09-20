"""Modelos de datos SQLAlchemy para Kontable."""

import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    Column,
    String,
    Boolean,
    DateTime,
    Date,
    Numeric,
    Integer,
    SmallInteger,
    BigInteger,
    ForeignKey,
    Text,
    UniqueConstraint,
    CheckConstraint,
    Index,
    false,
)
from sqlalchemy.orm import relationship
from dian_automation.db.database import Base


def generate_uuid() -> str:
    """Generador de UUIDv4 como string."""
    return str(uuid.uuid4())


# Tipo de negocio (Story 6.1): origen de las cifras. Son códigos de datos, no estados en español (ADR-006).
INCOME_SOURCE_DIAN = "DIAN"  # factura electrónicamente: cifras e impuestos desde el informe de la DIAN
INCOME_SOURCE_MANUAL_SALES = "MANUAL_SALES"  # registra sus ventas a mano; sin IVA ni ICA
INCOME_SOURCES = (INCOME_SOURCE_DIAN, INCOME_SOURCE_MANUAL_SALES)

# Perfil tributario de un negocio DIAN: periodicidad del IVA (NULL = no responsable de IVA).
IVA_PERIODICITY_BIMESTRAL = "BIMESTRAL"
IVA_PERIODICITY_CUATRIMESTRAL = "CUATRIMESTRAL"
IVA_PERIODICITIES = (IVA_PERIODICITY_BIMESTRAL, IVA_PERIODICITY_CUATRIMESTRAL)


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    email = Column(String(255), unique=True, nullable=False, index=True)
    full_name = Column(String(255), nullable=False)
    phone = Column(String(30), nullable=True)
    role = Column(String(20), nullable=False, default="CLIENT", index=True)  # CLIENT, ADMIN, TECH_OPS
    telegram_chat_id = Column(BigInteger, unique=True, nullable=True, index=True)
    telegram_username = Column(String(100), nullable=True)
    is_telegram_linked = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    businesses = relationship("Business", back_populates="client", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="client", cascade="all, delete-orphan")
    link_tokens = relationship("TelegramLinkToken", back_populates="user", cascade="all, delete-orphan")
    otp_codes = relationship("OTPCode", back_populates="user", cascade="all, delete-orphan")


class Business(Base):
    __tablename__ = "businesses"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    client_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    legal_name = Column(String(255), nullable=False)
    commercial_name = Column(String(255), nullable=False)
    nit = Column(String(30), nullable=False, index=True)
    dv = Column(String(2), nullable=False)
    taxpayer_type = Column(String(20), default="PERSONA_NATURAL", nullable=False)  # PERSONA_NATURAL, PERSONA_JURIDICA
    legal_rep_doc = Column(String(30), nullable=True)
    economic_activity = Column(String(255), nullable=True)
    invoice_prefix_filter = Column(String(50), nullable=True)
    income_source = Column(
        String(20), nullable=False, default=INCOME_SOURCE_DIAN, server_default=INCOME_SOURCE_DIAN
    )  # DIAN, MANUAL_SALES
    iva_periodicity = Column(String(20), nullable=True)  # BIMESTRAL, CUATRIMESTRAL; NULL = sin IVA
    is_withholding_agent = Column(Boolean, nullable=False, default=False, server_default=false())
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    client = relationship("User", back_populates="businesses")
    invoices = relationship("Invoice", back_populates="business", cascade="all, delete-orphan")
    summaries = relationship("MonthlyTaxSummary", back_populates="business", cascade="all, delete-orphan")
    extraction_jobs = relationship("DIANExtractionJob", back_populates="business", cascade="all, delete-orphan")


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    business_id = Column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id = Column(String(36), nullable=True)
    document_type = Column(String(100), nullable=False)  # Factura electrónica, Nota de crédito electrónica, etc.
    cufe = Column(String(255), unique=True, nullable=False, index=True)
    folio = Column(String(50), nullable=True)
    prefix = Column(String(50), nullable=True)
    currency = Column(String(10), default="COP", nullable=False)
    payment_form = Column(String(50), nullable=True)
    payment_method = Column(String(50), nullable=True)
    issue_date = Column(DateTime, nullable=False, index=True)
    reception_date = Column(DateTime, nullable=True)
    issuer_nit = Column(String(30), nullable=False)
    issuer_name = Column(String(255), nullable=False)
    receiver_nit = Column(String(30), nullable=False)
    receiver_name = Column(String(255), nullable=False)

    # Impuestos y retenciones
    iva = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    ica = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    inc = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    timbre = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    inc_bolsas = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    in_carbono = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    in_combustibles = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    ibua = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    icui = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    rete_iva = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    rete_renta = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    rete_ica = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    total = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)

    dian_status = Column(String(100), nullable=True)
    group_type = Column(String(20), nullable=False, index=True)  # Emitido, Recibido
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    business = relationship("Business", back_populates="invoices")


class MonthlyTaxSummary(Base):
    __tablename__ = "monthly_tax_summaries"
    __table_args__ = (UniqueConstraint("business_id", "period_year_month", name="uq_business_period"),)

    id = Column(String(36), primary_key=True, default=generate_uuid)
    business_id = Column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    period_year_month = Column(String(7), nullable=False, index=True)  # YYYY-MM
    total_invoiced_net = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    iva_generado = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    iva_descontable = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    iva_balance = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)  # >0 Saldo a pagar, <0 Saldo a favor
    rete_iva_total = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    rete_renta_total = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    rete_ica_total = Column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    total_invoices_count = Column(Integer, default=0, nullable=False)
    variation_vs_previous_pct = Column(Numeric(6, 2), nullable=True)
    calculated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    business = relationship("Business", back_populates="summaries")


class DIANExtractionJob(Base):
    __tablename__ = "dian_extraction_jobs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    business_id = Column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    target_period = Column(String(30), nullable=False)  # 'YYYY-MM' o rango 'YYYY-MM-DD - YYYY-MM-DD'
    status = Column(String(20), default="ENQUEUED", nullable=False, index=True)  # ENQUEUED, PROCESSING, SUCCESS, RETRY_PENDING, FAILED
    attempt_count = Column(Integer, default=0, nullable=False)
    max_attempts = Column(Integer, default=3, nullable=False)
    next_run_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    zip_path = Column(String(500), nullable=True)
    error_code = Column(String(100), nullable=True)
    error_detail = Column(Text, nullable=True)
    screenshot_path = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    business = relationship("Business", back_populates="extraction_jobs")


class TelegramLinkToken(Base):
    """Tokens de un solo uso para vinculación de clientes por Deep Linking."""
    __tablename__ = "telegram_link_tokens"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    is_used = Column(Boolean, default=False, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="link_tokens")


class OTPCode(Base):
    """Códigos OTP de un solo uso para el login web; solo se guarda el hash HMAC, nunca el código."""
    __tablename__ = "otp_codes"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_hash = Column(String(255), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    is_used = Column(Boolean, default=False, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="otp_codes")

    __table_args__ = (Index("idx_otp_user_created", "user_id", "created_at"),)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    client_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan = Column(String(20), nullable=False)  # TRIMESTRAL, SEMESTRAL, ANUAL
    discount_rate = Column(Numeric(5, 2), nullable=False)  # 5.00, 8.00, 10.00
    base_price = Column(Numeric(14, 2), nullable=False)
    final_price = Column(Numeric(14, 2), nullable=False)
    start_date = Column(Date, nullable=False)
    cutoff_date = Column(Date, nullable=False, index=True)
    grace_period_end = Column(Date, nullable=False)  # cutoff_date + 3 días
    status = Column(String(20), default="ACTIVO", nullable=False, index=True)  # ACTIVO, EN_MORA, BLOQUEADO, CANCELADO
    last_notified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    client = relationship("User", back_populates="subscriptions")
    payments = relationship("PaymentRecord", back_populates="subscription", cascade="all, delete-orphan")


class PaymentRecord(Base):
    __tablename__ = "payment_records"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    subscription_id = Column(String(36), ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True)
    amount = Column(Numeric(14, 2), nullable=False)
    payment_date = Column(Date, nullable=False)
    payment_method = Column(String(50), default="TRANSFERENCIA", nullable=False)
    reference_code = Column(String(100), nullable=True)
    verified_by_admin_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    subscription = relationship("Subscription", back_populates="payments")


def _default_key_from_nit_digit(context) -> int:
    """Filas heredadas (solo `nit_last_digit`): la llave de un dígito es ese mismo dígito.

    Sin `nit_last_digit` devuelve None y el NOT NULL de la columna rechaza la fila, en lugar de guardarla
    en silencio como la terminación 0.
    """
    return context.get_current_parameters().get("nit_last_digit")


class DIANTaxCalendar(Base):
    """Fecha límite de una obligación del calendario tributario DIAN (Story 4.1a).

    Cada fila cubre un rango inclusivo de terminaciones del NIT (`key_from`..`key_to`) de `key_length`
    dígitos (0 = aplica sin importar el NIT). `installment` (0 = sin cuotas) y `jurisdiction`
    ('' = nacional) son centinelas y no NULL para que el índice único funcione.
    """

    __tablename__ = "dian_tax_calendar"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    tax_type = Column(String(50), nullable=False, index=True)  # IVA_BIMESTRAL, IVA_CUATRIMESTRAL, RETEFUENTE, RENTA_*...
    fiscal_year = Column(Integer, nullable=False, index=True)
    period_label = Column(String(100), nullable=False)  # 'Jul – Ago 2026'
    period_start = Column(Date, nullable=True)  # periodo que cubre el vencimiento
    period_end = Column(Date, nullable=True)
    key_length = Column(SmallInteger, nullable=False, default=1)  # 0, 1 o 2 dígitos de la terminación del NIT
    key_from = Column(SmallInteger, nullable=False, default=_default_key_from_nit_digit)
    key_to = Column(SmallInteger, nullable=False, default=_default_key_from_nit_digit)
    installment = Column(SmallInteger, nullable=False, default=0)  # 0 = sin cuotas
    jurisdiction = Column(String(60), nullable=False, default="")  # '' = nacional; municipio para ICA
    nit_last_digit = Column(Integer, nullable=True, index=True)  # 0-9; heredada, la usa el bot hasta la Story 4.1b
    deadline_date = Column(Date, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index(
            "uq_dian_tax_calendar_obligation",
            "tax_type", "fiscal_year", "period_label", "installment", "jurisdiction",
            "key_length", "key_from", "key_to",
            unique=True,
        ),
        Index("idx_dian_tax_calendar_lookup", "tax_type", "fiscal_year", "key_length", "key_from", "key_to"),
    )



class WorkerHeartbeat(Base):
    """Último contacto de cada worker con /internal/jobs/next (Story 1.8). La hora es la del servidor."""

    __tablename__ = "worker_heartbeats"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(100), unique=True, nullable=False)  # cabecera X-Worker-Name; 'remote' por defecto
    # Nulo solo en la fila que crea el monitor cuando ningún worker ha reportado nunca
    last_seen_at = Column(DateTime, nullable=True)
    last_alert_at = Column(DateTime, nullable=True)  # último aviso de silencio enviado
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Sale(Base):
    """Venta declarada por el cliente (total y descripción opcional), desde Telegram o Web."""

    __tablename__ = "sales"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    business_id = Column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    total_amount = Column(Numeric(14, 2), nullable=False)
    description = Column(String(500), nullable=True)
    recorded_via = Column(String(20), nullable=False)  # TELEGRAM, WEB
    recorded_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("total_amount > 0", name="ck_sales_total_positive"),
        Index("idx_sales_business_created", "business_id", "created_at"),
    )
