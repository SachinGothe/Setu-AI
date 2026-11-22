"""One-time local admin creation or promotion without deleting user data."""

import re
from datetime import datetime, timezone
from uuid import uuid4

from app.core.database import connection, initialize_database
from app.core.security import hash_password


_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def bootstrap_admin(email: str, password: str) -> str:
    normalized_email = email.strip().lower()
    if not _EMAIL.fullmatch(normalized_email):
        raise ValueError("ADMIN_EMAIL must be a valid email address.")
    if len(password) < 10 or len(password) > 128:
        raise ValueError("ADMIN_PASSWORD must be between 10 and 128 characters.")

    initialize_database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connection() as db:
        row = db.execute(
            "SELECT id,role,is_active FROM users WHERE email=? COLLATE NOCASE",
            (normalized_email,),
        ).fetchone()
        if row is None:
            user_id = str(uuid4())
            db.execute(
                "INSERT INTO users(id,email,display_name,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                (user_id, normalized_email, "CivicPilot Administrator", hash_password(password), "admin", now),
            )
            db.execute("INSERT INTO user_profiles(user_id,updated_at) VALUES(?,?)", (user_id, now))
            outcome = "created"
        else:
            if not row["is_active"]:
                raise ValueError("The matching account is inactive. Reactivate it through an authorized admin workflow first.")
            user_id = row["id"]
            db.execute(
                "UPDATE users SET role='admin',password_hash=? WHERE id=?",
                (hash_password(password), user_id),
            )
            # Previously issued tokens can carry a normal-user role until expiry; revoke them.
            db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            outcome = "promoted" if row["role"] != "admin" else "updated"

        db.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid4()), user_id, f"admin_account_{outcome}", "user", user_id, "{}", now),
        )
    return outcome
