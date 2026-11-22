import re
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.database import connection, initialize_database
from app.core.rate_limit import enforce_rate_limit
from app.core.security import create_access_token, current_user, hash_password, verify_password
from app.services.audit import record_audit


router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class RegisterPayload(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(min_length=2, max_length=100)


class LoginPayload(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=128)


def _public_user(row) -> dict:
    return {"id": row["id"], "email": row["email"], "display_name": row["display_name"], "role": row["role"], "created_at": row["created_at"]}


@router.post("/register", status_code=201)
def register(payload: RegisterPayload, request: Request) -> dict:
    enforce_rate_limit(request, "register", limit=5, window_seconds=3600)
    email = payload.email.strip().lower()
    if not _EMAIL.fullmatch(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    initialize_database()
    user_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with connection() as db:
            db.execute("INSERT INTO users(id,email,display_name,password_hash,role,created_at) VALUES(?,?,?,?, 'user', ?)", (
                user_id, email, payload.display_name.strip(), hash_password(payload.password), now,
            ))
            db.execute("INSERT INTO user_profiles(user_id,updated_at) VALUES(?,?)", (user_id, now))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="An account with this email already exists.") from exc
    record_audit(user_id, "account_registered", "user", user_id)
    token = create_access_token(user_id)
    return {"access_token": token, "token_type": "bearer", "user": {"id": user_id, "email": email, "display_name": payload.display_name.strip(), "role": "user", "created_at": now}}


@router.post("/login")
def login(payload: LoginPayload, request: Request) -> dict:
    enforce_rate_limit(request, "login", limit=10)
    initialize_database()
    with connection() as db:
        row = db.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE AND is_active=1", (payload.email.strip().lower(),)).fetchone()
    if row is None or not verify_password(payload.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    token = create_access_token(row["id"])
    record_audit(row["id"], "account_login", "user", row["id"])
    return {"access_token": token, "token_type": "bearer", "user": _public_user(row)}


@router.get("/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {key: value for key, value in user.items() if not key.startswith("_")}


@router.post("/logout")
def logout(user: dict = Depends(current_user)) -> dict:
    with connection() as db:
        db.execute("DELETE FROM auth_sessions WHERE id=?", (user["_session_id"],))
    record_audit(user["id"], "account_logout", "user", user["id"])
    return {"status": "logged_out"}
