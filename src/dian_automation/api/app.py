"""Aplicación principal FastAPI para el backend de Kontable."""

import os
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from dian_automation.config import config
from dian_automation.core.auth_service import AuthServiceError
from dian_automation.subscriptions.lockout_service import SubscriptionBlockedError
from dian_automation.api.routes_auth import router as auth_router
from dian_automation.api.routes_dashboard import router as dashboard_router
from dian_automation.api.routes_iva import router as iva_router
from dian_automation.api.routes_invoices import router as invoices_router
from dian_automation.api.routes_sales import router as sales_router
from dian_automation.api.routes_income import router as income_router
from dian_automation.api.routes_config import router as config_router
from dian_automation.api.routes_internal_worker import router as internal_worker_router
from dian_automation.api.routes_calendar import router as calendar_router

app = FastAPI(
    title="Kontable API",
    description="API REST de gestión tributaria DIAN, resúmenes contables y control de suscripción.",
    version="1.0.0",
)

# Configuración CORS permisiva para consumo desde el frontend móvil o navegadores locales
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(SubscriptionBlockedError)
async def subscription_blocked_exception_handler(request: Request, exc: SubscriptionBlockedError):
    """Manejador global para rechazar peticiones de suscripciones bloqueadas con 403 Forbidden."""
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content=exc.to_dict(),
    )


@app.exception_handler(AuthServiceError)
async def auth_service_exception_handler(request: Request, exc: AuthServiceError):
    """Traduce los errores de autenticación al envelope {"detail": ...}; el 401 lleva WWW-Authenticate."""
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == status.HTTP_401_UNAUTHORIZED else None
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=headers)


# Registrar routers
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(iva_router)
app.include_router(invoices_router)
app.include_router(sales_router)
app.include_router(income_router)
app.include_router(config_router)
app.include_router(internal_worker_router)
app.include_router(calendar_router)


def _resolve_frontend_dir() -> str:
    """Ubica la carpeta del prototipo web/móvil (ProyectoDianFront) para servirla como estático.

    Permite fijar FRONTEND_DIR explícitamente (útil en despliegue), y si no se define,
    intenta la ruta relativa por defecto usada en desarrollo local (carpeta hermana de este repo).
    """
    if config.frontend_dir:
        return config.frontend_dir
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "ProyectoDianFront")
    )


_frontend_dir = _resolve_frontend_dir()
if os.path.isdir(_frontend_dir):
    app.mount("/app", StaticFiles(directory=_frontend_dir, html=True), name="frontend")


@app.get("/", tags=["General"])
def root():
    return {
        "app": "Kontable API",
        "version": "1.0.0",
        "status": "online",
        "docs_url": "/docs",
    }


@app.get("/health", tags=["General"])
def health_check():
    return {"status": "ok", "service": "kontable-backend"}


@app.get("/servicio-suspendido", tags=["General"])
def servicio_suspendido_view():
    return {
        "status": "suspended",
        "error": "SUBSCRIPTION_BLOCKED",
        "message": (
            "Tu suscripción se encuentra suspendida temporalmente por pago pendiente. "
            "Comunícate con Katerinn para reactivar tus reportes."
        ),
        "contact_whatsapp": "+57 300 123 4567",
        "admin": "Katerinn",
    }
