from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import admin_user
from app.services import source_refresh, source_registry


router = APIRouter(prefix="/api/v1", tags=["sources"])


class SourceCreate(BaseModel):
    display_name: str = Field(min_length=2, max_length=120)
    official_url: str = Field(min_length=8, max_length=500)
    department: str = Field(default="", max_length=160)
    source_type: Literal["official_api", "web_page", "pdf", "manual_upload"] = "web_page"
    permission_notes: str = Field(default="", max_length=2000)


class SourceUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=120)
    official_url: str | None = Field(default=None, min_length=8, max_length=500)
    department: str | None = Field(default=None, max_length=160)
    source_type: Literal["official_api", "web_page", "pdf", "manual_upload"] | None = None
    status: Literal["pending_verification", "verified", "disabled"] | None = None
    enabled: bool | None = None
    approved_urls: list[str] | None = None
    fetch_strategy: str | None = Field(default=None, max_length=80)
    permission_notes: str | None = Field(default=None, max_length=2000)
    rate_limit_seconds: int | None = Field(default=None, ge=60, le=2592000)


@router.get("/sources")
def sources() -> dict:
    items = source_registry.list_sources()
    for item in items:
        item["health_status"] = source_refresh.health_status(item)
    return {"items": items, "connector_status": "nsp_homepage_only"}


@router.post("/admin/sources")
def create_source(payload: SourceCreate, admin: dict = Depends(admin_user)) -> dict:
    raise HTTPException(status_code=410, detail="Government source registry is read-only in this local demo.")


@router.patch("/admin/sources/{source_id}")
def patch_source(source_id: str, payload: SourceUpdate, admin: dict = Depends(admin_user)) -> dict:
    raise HTTPException(status_code=410, detail="Government source registry is read-only in this local demo.")


@router.post("/admin/sources/{source_id}/refresh")
def refresh_source(source_id: str, admin: dict = Depends(admin_user)) -> dict:
    raise HTTPException(status_code=410, detail="Live government-source retrieval is disabled by local demo policy; no network request was made.")


@router.post("/admin/sources/refresh-all")
def refresh_all(admin: dict = Depends(admin_user)) -> dict:
    raise HTTPException(status_code=410, detail="Live government-source retrieval is disabled by local demo policy; no network request was made.")


@router.get("/admin/sources/{source_id}/history")
def source_history(source_id: str, admin: dict = Depends(admin_user)) -> dict:
    if source_registry.get_source(source_id) is None:
        raise HTTPException(status_code=404, detail="Source not found.")
    return {"items": source_registry.source_history(source_id)}
