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
