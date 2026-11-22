"""Small SQLite-backed registry for candidate public information sources."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings


SEED_SOURCES = [
    ("PM Vishwakarma", "https://pmvishwakarma.gov.in/", "Government of India", "web_page"),
    ("National Scholarship Portal", "https://scholarships.gov.in/", "Government of India", "web_page"),
    ("myScheme", "https://www.myscheme.gov.in/", "Government of India", "web_page"),
    ("DigiLocker", "https://www.digilocker.gov.in/", "Government of India", "web_page"),
]


def _database_path() -> Path:
    configured = Path(get_settings().source_database_path)
    path = configured if configured.is_absolute() else Path(__file__).resolve().parents[2] / configured
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_database_path(), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def initialize_registry() -> None:
    with _connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS government_sources (
            id TEXT PRIMARY KEY, display_name TEXT NOT NULL, official_url TEXT NOT NULL,
            department TEXT NOT NULL DEFAULT '', source_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_verification', enabled INTEGER NOT NULL DEFAULT 0,
            approved_urls TEXT NOT NULL DEFAULT '[]', fetch_strategy TEXT NOT NULL DEFAULT 'manual_review',
            permission_notes TEXT NOT NULL DEFAULT '', rate_limit_seconds INTEGER NOT NULL DEFAULT 86400,
            created_at TEXT NOT NULL, last_checked_at TEXT, last_success_at TEXT,
            last_content_change_at TEXT, last_http_status INTEGER, last_error TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS source_fetch_runs (
            id TEXT PRIMARY KEY, source_id TEXT NOT NULL, started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL, status TEXT NOT NULL, http_status INTEGER,
            error TEXT, source_url TEXT, content_sha256 TEXT, content_title TEXT,
            FOREIGN KEY(source_id) REFERENCES government_sources(id)
        )""")
        _add_columns(db, "government_sources", {
            "content_title": "TEXT", "content_text": "TEXT", "content_sha256": "TEXT",
        })
        _add_columns(db, "source_fetch_runs", {
            "source_url": "TEXT", "content_sha256": "TEXT", "content_title": "TEXT",
        })
        count = db.execute("SELECT COUNT(*) FROM government_sources").fetchone()[0]
        if count == 0:
            now = _now()
            for name, url, department, kind in SEED_SOURCES:
                db.execute(
                    "INSERT INTO government_sources(id,display_name,official_url,department,source_type,created_at) VALUES(?,?,?,?,?,?)",
                    (str(uuid4()), name, url, department, kind, now),
                )


def _add_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for column, kind in columns.items():
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    data["approved_urls"] = json.loads(data["approved_urls"])
    data["content_available"] = bool(data.get("content_text"))
    data.pop("content_text", None)
    return data


def list_sources() -> list[dict]:
    initialize_registry()
    with _connect() as db:
        return [_source(row) for row in db.execute("SELECT * FROM government_sources ORDER BY display_name")]


def get_source(source_id: str) -> dict | None:
    initialize_registry()
    with _connect() as db:
        row = db.execute("SELECT * FROM government_sources WHERE id=?", (source_id,)).fetchone()
        return _source(row) if row else None


def add_source(data: dict) -> dict:
    initialize_registry()
    source_id = str(uuid4())
    with _connect() as db:
        db.execute(
            "INSERT INTO government_sources(id,display_name,official_url,department,source_type,permission_notes,created_at) VALUES(?,?,?,?,?,?,?)",
            (source_id, data["display_name"], data["official_url"], data.get("department", ""), data["source_type"], data.get("permission_notes", ""), _now()),
        )
    return get_source(source_id)  # type: ignore[return-value]


def update_source(source_id: str, updates: dict) -> dict | None:
    initialize_registry()
    current = get_source(source_id)
    if current is None:
        return None
    if updates.get("status") == "verified" and (not updates.get("permission_notes", current["permission_notes"]).strip() or not updates.get("approved_urls", current["approved_urls"])):
        raise ValueError("Verification requires permission notes and at least one approved URL.")
    if updates.get("enabled") and updates.get("status", current["status"]) != "verified":
        raise ValueError("A source must be verified before it can be enabled.")
    mapping = {"display_name": "display_name", "official_url": "official_url", "department": "department", "source_type": "source_type", "status": "status", "enabled": "enabled", "approved_urls": "approved_urls", "fetch_strategy": "fetch_strategy", "permission_notes": "permission_notes", "rate_limit_seconds": "rate_limit_seconds"}
    fields = [(mapping[key], json.dumps(value) if key == "approved_urls" else int(value) if key == "enabled" else value) for key, value in updates.items() if key in mapping]
    if fields:
        with _connect() as db:
            db.execute(f"UPDATE government_sources SET {', '.join(f'{key}=?' for key, _ in fields)} WHERE id=?", [value for _, value in fields] + [source_id])
    return get_source(source_id)


def begin_refresh(source_id: str) -> dict:
    """Reserve the source refresh slot and enforce the configured interval."""
    now = _now()
    run_id = str(uuid4())
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        source = db.execute("SELECT * FROM government_sources WHERE id=?", (source_id,)).fetchone()
        if source is None:
            raise KeyError("Source not found.")
        if source["status"] != "verified" or not source["enabled"]:
            raise PermissionError("Only verified, enabled sources can be refreshed.")
        previous = source["last_checked_at"]
        if previous:
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(previous)
            if elapsed < timedelta(seconds=source["rate_limit_seconds"]):
                error = "Refresh skipped because the source rate limit has not elapsed."
                db.execute("INSERT INTO source_fetch_runs(id,source_id,started_at,completed_at,status,error,source_url) VALUES(?,?,?,?,?,?,?)", (run_id, source_id, now, now, "rate_limited", error, source["official_url"]))
                db.execute("UPDATE government_sources SET last_checked_at=? WHERE id=?", (now, source_id))
                return {"id": run_id, "source_id": source_id, "started_at": now, "completed_at": now, "status": "rate_limited", "http_status": None, "error": error}
        db.execute("INSERT INTO source_fetch_runs(id,source_id,started_at,completed_at,status,source_url) VALUES(?,?,?,?,?,?)", (run_id, source_id, now, now, "fetching", source["official_url"]))
        db.execute("UPDATE government_sources SET last_checked_at=?,last_error=NULL WHERE id=?", (now, source_id))
    return {"id": run_id, "source_id": source_id, "started_at": now, "completed_at": None, "status": "fetching", "http_status": None, "error": None}


def record_unavailable_connector(source_id: str, error: str) -> dict:
    """Record an admin refresh attempt for a source without an implemented connector."""
    now = _now()
    run_id = str(uuid4())
    with _connect() as db:
        source = db.execute("SELECT official_url FROM government_sources WHERE id=?", (source_id,)).fetchone()
        if source is None:
            raise KeyError("Source not found.")
        db.execute("INSERT INTO source_fetch_runs(id,source_id,started_at,completed_at,status,error,source_url) VALUES(?,?,?,?,?,?,?)", (run_id, source_id, now, now, "connector_unavailable", error[:1000], source["official_url"]))
        db.execute("UPDATE government_sources SET last_checked_at=?,last_http_status=NULL,last_error=? WHERE id=?", (now, error[:1000], source_id))
    return {"id": run_id, "source_id": source_id, "started_at": now, "completed_at": now, "status": "connector_unavailable", "http_status": None, "error": error}


def finish_refresh_success(run_id: str, source_id: str, page) -> dict:
    now = _now()
    with _connect() as db:
        started_at = db.execute("SELECT started_at FROM source_fetch_runs WHERE id=?", (run_id,)).fetchone()[0]
        previous_hash = db.execute("SELECT content_sha256 FROM government_sources WHERE id=?", (source_id,)).fetchone()[0]
        changed_at = now if previous_hash != page.content_sha256 else None
        if changed_at is None:
            db.execute("UPDATE government_sources SET last_success_at=?,last_http_status=?,last_error=NULL,content_title=?,content_text=?,content_sha256=? WHERE id=?", (page.retrieved_at, page.http_status, page.title, page.content, page.content_sha256, source_id))
        else:
            db.execute("UPDATE government_sources SET last_success_at=?,last_content_change_at=?,last_http_status=?,last_error=NULL,content_title=?,content_text=?,content_sha256=? WHERE id=?", (page.retrieved_at, changed_at, page.http_status, page.title, page.content, page.content_sha256, source_id))
        db.execute("UPDATE source_fetch_runs SET completed_at=?,status='success',http_status=?,source_url=?,content_sha256=?,content_title=? WHERE id=?", (now, page.http_status, page.url, page.content_sha256, page.title, run_id))
    return {"id": run_id, "source_id": source_id, "started_at": started_at, "completed_at": now, "status": "success", "http_status": page.http_status, "error": None, "source_url": page.url, "content_sha256": page.content_sha256, "content_title": page.title, "content_ref": f"sqlite:government_sources/{source_id}"}


def finish_refresh_failure(run_id: str, source_id: str, error: str, status_code: int | None = None) -> dict:
    now = _now()
    with _connect() as db:
        db.execute("UPDATE government_sources SET last_http_status=?,last_error=? WHERE id=?", (status_code, error[:1000], source_id))
        db.execute("UPDATE source_fetch_runs SET completed_at=?,status='failed',http_status=?,error=? WHERE id=?", (now, status_code, error[:1000], run_id))
        row = db.execute("SELECT * FROM source_fetch_runs WHERE id=?", (run_id,)).fetchone()
        return dict(row)


def source_history(source_id: str) -> list[dict]:
    initialize_registry()
    with _connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM source_fetch_runs WHERE source_id=? ORDER BY started_at DESC LIMIT 100", (source_id,))]
