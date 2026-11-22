import json
from datetime import datetime, timezone
from uuid import uuid4

from app.core.database import connection, initialize_database


def record_audit(actor_id: str | None, action: str, entity_type: str, entity_id: str | None = None, details: dict | None = None) -> None:
    initialize_database()
    with connection() as db:
        db.execute("INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)", (
            str(uuid4()), actor_id, action, entity_type, entity_id,
            json.dumps(details or {}, ensure_ascii=False), datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ))
