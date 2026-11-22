"""Role-protected local administration. Government source controls stay read-only."""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.database import connection
from app.core.security import admin_user
from app.services.audit import record_audit


router = APIRouter(prefix="/api/v1/admin", tags=["administration"])


class ActivePayload(BaseModel):
    active: bool


class RolePayload(BaseModel):
    role: str = Field(pattern="^(user|admin)$")


def _admin_count(db) -> int:
    return db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1").fetchone()[0]


@router.get("/overview")
def overview(admin: dict = Depends(admin_user)) -> dict:
    with connection() as db:
        counts = {
            "total_registered_users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "total_uploaded_legal_documents": db.execute("SELECT COUNT(*) FROM documents WHERE is_sample=0 AND (mime_type='application/pdf' OR document_kind='knowledge')").fetchone()[0],
            "total_knowledge_documents": db.execute("SELECT COUNT(*) FROM documents WHERE document_kind='knowledge' AND is_sample=0").fetchone()[0],
            "total_sample_schemes": db.execute("SELECT COUNT(*) FROM schemes WHERE demo_only=1 AND is_active=1").fetchone()[0],
        }
        recent = db.execute("SELECT id,actor_user_id,action,entity_type,entity_id,details_json,created_at FROM audit_logs ORDER BY created_at DESC LIMIT 100").fetchall()
    return {
        "counts": counts,
        "recent_activities": [dict(row) for row in recent],
        "source_controls": "read_only; live government fetching disabled",
    }


@router.get("/users")
def list_users(admin: dict = Depends(admin_user)) -> dict:
    with connection() as db:
        rows = db.execute("SELECT id,email,display_name,role,created_at,is_active FROM users ORDER BY created_at DESC").fetchall()
    return {"items": [dict(row) for row in rows]}


@router.get("/users/{user_id}")
def user_detail(user_id: str, admin: dict = Depends(admin_user)) -> dict:
    with connection() as db:
        row = db.execute("SELECT id,email,display_name,role,created_at,is_active FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="User not found.")
        counts = {
            "documents": db.execute("SELECT COUNT(*) FROM documents WHERE owner_user_id=? AND is_sample=0", (user_id,)).fetchone()[0],
            "applications": db.execute("SELECT COUNT(*) FROM applications WHERE user_id=?", (user_id,)).fetchone()[0],
        }
    # Do not expose uploaded document content or authentication/session fields.
    return {"user": dict(row), "counts": counts}


@router.patch("/users/{user_id}/active")
def set_user_active(user_id: str, payload: ActivePayload, admin: dict = Depends(admin_user)) -> dict:
    if user_id == admin["id"] and not payload.active:
        raise HTTPException(status_code=409, detail="Administrators cannot deactivate their own account.")
    with connection() as db:
        target = db.execute("SELECT role,is_active FROM users WHERE id=?", (user_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found.")
        if not payload.active and target["role"] == "admin" and target["is_active"] and _admin_count(db) <= 1:
            raise HTTPException(status_code=409, detail="The last active administrator cannot be deactivated.")
        db.execute("UPDATE users SET is_active=? WHERE id=?", (int(payload.active), user_id))
        if not payload.active:
            db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
    record_audit(admin["id"], "user_activation_changed", "user", user_id, {"active": payload.active})
    return {"user_id": user_id, "active": payload.active}


@router.patch("/users/{user_id}/role")
def set_user_role(user_id: str, payload: RolePayload, admin: dict = Depends(admin_user)) -> dict:
    if user_id == admin["id"] and payload.role != "admin":
        raise HTTPException(status_code=409, detail="Administrators cannot remove their own admin role here.")
    with connection() as db:
        target = db.execute("SELECT role,is_active FROM users WHERE id=?", (user_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found.")
        if payload.role == target["role"]:
            return {"user_id": user_id, "role": payload.role}
        if payload.role == "user" and target["role"] == "admin" and target["is_active"] and _admin_count(db) <= 1:
            raise HTTPException(status_code=409, detail="The last active administrator cannot be demoted.")
        db.execute("UPDATE users SET role=? WHERE id=?", (payload.role, user_id))
        db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
    record_audit(admin["id"], "user_role_changed", "user", user_id, {"role": payload.role})
    return {"user_id": user_id, "role": payload.role}


@router.get("/audit-logs")
def audit_logs(limit: int = 100, admin: dict = Depends(admin_user)) -> dict:
    limit = max(1, min(limit, 500))
    with connection() as db:
        rows = db.execute("SELECT id,actor_user_id,action,entity_type,entity_id,details_json,created_at FROM audit_logs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [dict(row) for row in rows]}
