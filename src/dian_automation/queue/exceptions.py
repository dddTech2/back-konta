"""Jerarquía de excepciones tipificadas para errores de extracción DIAN."""


class DIANExtractionError(Exception):
    """Excepción base para fallos durante la extracción en el portal DIAN."""

    def __init__(self, message: str, code: str = "EXTRACTION_ERROR"):
        super().__init__(message)
        self.code = code
        self.message = message


class AuthFailedError(DIANExtractionError):
    """Credenciales erróneas, token inválido o rechazo de autenticación en la DIAN."""

    def __init__(self, message: str = "Fallo de autenticación en portal DIAN"):
        super().__init__(message, code="AUTH_FAILED")


class MailTokenTimeoutError(DIANExtractionError):
    """No se recibió el correo con el enlace mágico en Stalwart IMAP tras el tiempo límite."""

    def __init__(self, message: str = "Tiempo de espera agotado buscando token en correo Stalwart"):
        super().__init__(message, code="MAIL_TIMEOUT")


class TurnstileBlockedError(DIANExtractionError):
    """Cloudflare Turnstile bloqueó el acceso o no pudo resolverse."""

    def __init__(self, message: str = "Bloqueo o verificación fallida en Cloudflare Turnstile"):
        super().__init__(message, code="TURNSTILE_BLOCKED")


class DIANPortalDownError(DIANExtractionError):
    """El portal de la DIAN está fuera de línea, en mantenimiento o responde errores 5xx."""

    def __init__(self, message: str = "El portal DIAN VPFE no responde o se encuentra en mantenimiento"):
        super().__init__(message, code="DIAN_DOWN")


class ExportTimeoutError(DIANExtractionError):
    """La generación del reporte en la tabla de exportaciones de la DIAN tardó más de lo permitido."""

    def __init__(self, message: str = "Tiempo de espera agotado esperando generación de reporte en DIAN"):
        super().__init__(message, code="EXPORT_TIMEOUT")


# Fallos "lentos": la DIAN no entregó el archivo a tiempo o el trabajo quedó atascado en PROCESSING.
# Se reintentan a las 6 h solo para ese trabajo (Story 1.6). Cada excepción llega con dos nombres: el de
# la clase (worker remoto: type(e).__name__) y su código (worker local: e.code). Todo lo demás es un
# fallo duro (Story 1.3): agregar una excepción nueva exige clasificarla aquí.
STALE_PROCESSING_CODE = "STALE_PROCESSING"
SLOW_ERROR_CODES = frozenset({"ExportTimeoutError", "EXPORT_TIMEOUT", STALE_PROCESSING_CODE})


def is_slow_error(error_code) -> bool:
    """True si el código corresponde a un fallo lento (reintento a 6 h, sin pausar la cola)."""
    return error_code in SLOW_ERROR_CODES
