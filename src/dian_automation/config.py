import os
from dataclasses import dataclass
from dotenv import load_dotenv

# Cargar variables de entorno desde .env
load_dotenv()

@dataclass(frozen=True)
class DianSelectors:
    PERSON_CODE: str = "#PersonCode"
    REPRESENTATIVE_TAB_BUTTON: str = 'button:has-text("Representante legal"), button:has-text("Representante Legal")'
    REPRESENTATIVE_CODE: str = "#UserCode:visible"
    COMPANY_CODE: str = "#CompanyCode:visible"
    LOGIN_BUTTON: str = '#login-auth-user:visible, button:has-text("Entrar"):visible, button:has-text("Entrar")'
    HISTORICO_LINK: str = 'a:has-text("Histórico")'
    DESCARGA_LISTADOS_LINK: str = 'a:has-text("Descarga de listados")'
    EXPORT_RANGE: str = "#export-range"
    ACTION_LEFT: str = "#action-left"
    ACTION_RIGHT: str = "#action-right"
    EXPORT_EXCEL_BUTTON: str = 'button:has-text("Exportar Excel")'
    CONFIRM_SI_BUTTON: str = '#confirmModal-confirm-button'
    CLOSE_MODAL_SPAN: str = '#confirmModal .close, .modal.show .close, button:has-text("Aceptar"), span:has-text("×")'
    TABLE_EXPORT: str = "#tableExport"
    TABLE_EXPORT_ROWS: str = "#tableExport tbody tr"
    DOWNLOAD_LINK: str = 'a[href*="DownloadExportedZipFile"], a[title*="Descargar"], a:has(.fa-download)'
    STATUS_READY_ICON: str = 'i.fa-check, [title="Listo"], [data-original-title="Listo"]'

@dataclass(frozen=True)
class AppConfig:
    login_type: str = os.getenv("DIAN_LOGIN_TYPE", "persona").lower()
    dian_url: str = os.getenv("DIAN_URL", "https://catalogo-vpfe-hab.dian.gov.co/User/PersonLogin")
    dian_company_url: str = os.getenv("DIAN_COMPANY_URL", "https://catalogo-vpfe-hab.dian.gov.co/User/CompanyLogin")
    dian_person_code: str = os.getenv("DIAN_PERSON_CODE", "1000000001")
    dian_representative_code: str = os.getenv("DIAN_REPRESENTATIVE_CODE", "")
    dian_company_nit: str = os.getenv("DIAN_COMPANY_NIT", "")
    
    stalwart_host: str = os.getenv("STALWART_IMAP_HOST", "mail.example.com")
    stalwart_port: int = int(os.getenv("STALWART_IMAP_PORT", "993"))
    stalwart_user: str = os.getenv("STALWART_USER", "token@example.com")
    stalwart_password: str = os.getenv("STALWART_PASSWORD", "")
    
    headless: bool = os.getenv("HEADLESS", "False").lower() in ("true", "1", "yes")
    email_timeout_seconds: int = int(os.getenv("EMAIL_TIMEOUT_SECONDS", "60"))
    export_download_timeout_seconds: int = int(os.getenv("EXPORT_DOWNLOAD_TIMEOUT_SECONDS", "300"))
    # Un trabajo PROCESSING con más de esto sin respuesta se recupera como fallo lento (Story 1.6)
    stale_processing_seconds: int = int(
        os.getenv("STALE_PROCESSING_SECONDS") or (int(os.getenv("EXPORT_DOWNLOAD_TIMEOUT_SECONDS") or "300") + 600)
    )
    download_dir: str = os.getenv("DOWNLOAD_DIR", "./downloads")

    # Programador semanal de descargas (Story 1.7). Nace apagado: solo 'true' lo activa.
    scheduler_enabled: bool = os.getenv("SCHEDULER_ENABLED", "false").strip().lower() == "true"
    # 0 = lunes ... 6 = domingo (numeración de Python); hora de America/Bogota
    scheduler_weekday: int = int(os.getenv("SCHEDULER_WEEKDAY") or "6")
    scheduler_hour: int = int(os.getenv("SCHEDULER_HOUR") or "3")

    # Frontend web/móvil (para servir el prototipo y enlazarlo desde Telegram)
    frontend_dir: str = os.getenv("FRONTEND_DIR", "")
    kontable_web_url: str = os.getenv("KONTABLE_WEB_URL", "http://127.0.0.1:8000/app/")

    # Worker remoto (ej. corriendo en una red residencial fuera de la IP bloqueada del VPS)
    internal_worker_token: str = os.getenv("INTERNAL_WORKER_TOKEN", "")

    # Autenticación web (JWT Bearer tras el OTP por Telegram). Sin JWT_SECRET la app arranca, pero
    # emitir o validar un JWT falla: este módulo lo importan también el bot y el worker.
    jwt_secret: str = os.getenv("JWT_SECRET", "")
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = int(os.getenv("JWT_TTL_MINUTES") or "60")
    
    selectors: DianSelectors = DianSelectors()

config = AppConfig()
