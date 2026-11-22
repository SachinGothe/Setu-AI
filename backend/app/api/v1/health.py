from fastapi import APIRouter
from app.core.config import get_settings


router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "mode": "local_demo" if not settings.gemini_api_key else "configured",
        "gemini_configured": bool(settings.gemini_api_key),
    }


@router.get("/config/public")
def public_config() -> dict:
    settings = get_settings()
    return {
        "app_name": "CivicPilot AI",
        "languages": ["en", "kn", "hi"],
        "mode": "local_demo" if not settings.gemini_api_key else "configured",
        "disclaimer": "CivicPilot AI is not a government website or government-approved service.",
    }
