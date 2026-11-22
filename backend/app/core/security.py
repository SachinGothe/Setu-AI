"""Password hashing and signed local bearer tokens using the Python standard library."""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings
from app.core.database import connection, initialize_database


_bearer = HTTPBearer(auto_error=False)
_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def create_access_token(user_id: str) -> str:
    settings = get_settings()
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    session_id = str(uuid4())
    expires_at = int(time.time()) + settings.token_expiry_hours * 3600
    payload = _b64(json.dumps({"sub": user_id, "sid": session_id, "exp": expires_at}, separators=(",", ":")).encode())
    signature = _b64(hmac.new(settings.app_secret_key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    with connection() as db:
        db.execute("INSERT INTO auth_sessions(id,user_id,expires_at,created_at) VALUES(?,?,?,?)", (session_id, user_id, expires_at, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return f"{header}.{payload}.{signature}"


def _decode_token(token: str) -> tuple[str, str] | None:
    try:
        header, payload, signature = token.split(".")
        expected = _b64(hmac.new(get_settings().app_secret_key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        header_data = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
        if header_data != {"alg": "HS256", "typ": "JWT"}:
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if not isinstance(data.get("sub"), str) or not isinstance(data.get("sid"), str):
            return None
        if isinstance(data.get("exp"), bool) or int(data["exp"]) <= int(time.time()):
            return None
        return str(data["sub"]), str(data["sid"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Sign in to continue.", headers={"WWW-Authenticate": "Bearer"})
    token_data = _decode_token(credentials.credentials)
    if token_data is None:
        raise HTTPException(status_code=401, detail="Your session is invalid or expired. Sign in again.", headers={"WWW-Authenticate": "Bearer"})
    user_id, session_id = token_data
    initialize_database()
    with connection() as db:
        row = db.execute("SELECT id,email,display_name,role,created_at FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
        session = db.execute("SELECT 1 FROM auth_sessions WHERE id=? AND user_id=? AND expires_at>?", (session_id, user_id, int(time.time()))).fetchone()
    if row is None or session is None:
        raise HTTPException(status_code=401, detail="This account is unavailable.")
    user = dict(row)
    user["_session_id"] = session_id
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return user
