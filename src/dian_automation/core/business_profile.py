"""Reglas y helpers de perfil de negocio y paquetes Konta (Story 10.1).

Única fuente de verdad de las reglas de visibilidad y coherencia de paquetes.
Ningún módulo debe comparar `income_source` o `vat_responsible` a mano para decidir visibilidad.
"""

from typing import Any, Optional

from dian_automation.db.models import (
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    INCOME_SOURCE_MIXED,
    INCOME_SOURCES,
    PACKAGE_CON_IVA,
    PACKAGE_SIN_IVA,
    PACKAGES,
)


class ProfileError(Exception):
    """Error de validación en la coherencia del perfil de negocio."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _field(business: Any, name: str) -> Any:
    if isinstance(business, dict):
        return business.get(name)
    return getattr(business, name, None)


def _is_vat_responsible(business: Any) -> bool:
    """`vat_responsible` del negocio; si aún no tiene valor (objeto sin guardar), se deriva como el
    default del modelo: un facturador (DIAN) es declarante de IVA."""
    is_vat = _field(business, "vat_responsible")
    if is_vat is None:
        return (_field(business, "income_source") or INCOME_SOURCE_DIAN) == INCOME_SOURCE_DIAN
    return bool(is_vat)


def package_of(business: Any) -> str:
    """Calcula el paquete Konta del negocio: 'CON_IVA' si es responsable de IVA, de lo contrario 'SIN_IVA'."""
    if business is None:
        return PACKAGE_SIN_IVA
    return PACKAGE_CON_IVA if _is_vat_responsible(business) else PACKAGE_SIN_IVA


def has_einvoicing(business: Any) -> bool:
    """Indica si el negocio emite o consulta facturación electrónica (DIAN o MIXED)."""
    if business is None:
        return False
    source = _field(business, "income_source")
    return source in (INCOME_SOURCE_DIAN, INCOME_SOURCE_MIXED)


def has_manual_sales(business: Any) -> bool:
    """Indica si el negocio registra ventas manuales (MANUAL_SALES o MIXED)."""
    if business is None:
        return False
    source = _field(business, "income_source")
    return source in (INCOME_SOURCE_MANUAL_SALES, INCOME_SOURCE_MIXED)


def shows_iva(business: Any) -> bool:
    """Indica si el negocio es responsable de IVA y debe ver métricas y liquidación de IVA."""
    if business is None:
        return False
    return _is_vat_responsible(business)


def validate_profile(vat_responsible: bool, income_source: str) -> None:
    """Valida la coherencia de perfil tributario y módulo de ingresos.

    Lanza ProfileError("INVALID_INCOME_SOURCE") si el origen de ingresos no existe.
    Lanza ProfileError("VAT_REQUIRES_EINVOICING", ...) si el negocio es responsable de IVA y su perfil no es DIAN.
    """
    if income_source not in INCOME_SOURCES:
        raise ProfileError(
            "INVALID_INCOME_SOURCE",
            f"Origen de ingresos '{income_source}' inválido.",
        )

    if vat_responsible and income_source != INCOME_SOURCE_DIAN:
        raise ProfileError(
            "VAT_REQUIRES_EINVOICING",
            "Un declarante de IVA siempre factura electrónicamente: su perfil debe ser Facturador",
        )
