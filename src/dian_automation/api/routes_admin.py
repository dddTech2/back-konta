"""Endpoints de administración web para Konta."""

from typing import Any, Dict

from fastapi import APIRouter, Depends

from dian_automation.api.dependencies import get_current_admin
from dian_automation.db.models import User

router = APIRouter(prefix="/api/admin", tags=["Admin"])


@router.get("/ping")
def ping(admin: User = Depends(get_current_admin)) -> Dict[str, Any]:
    """Endpoint de salud protegido para verificar sesión y rol administrativo (Story 8.2)."""
    return {"ok": True}
