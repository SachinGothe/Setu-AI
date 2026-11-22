"""SQLite persistence shared by the local demo and source registry."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from app.core.config import get_settings


def database_path() -> Path:
    configured = Path(get_settings().database_path)
    path = configured if configured.is_absolute() else Path(__file__).resolve().parents[2] / configured
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(database_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_database() -> None:
    settings = get_settings()
    if settings.app_env.lower() == "production" and settings.app_secret_key == "replace_me":
        raise RuntimeError("APP_SECRET_KEY must be set to a private value in production.")
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')),
            created_at TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            age INTEGER, state TEXT, annual_family_income REAL, occupation TEXT,
            education_level TEXT, employment_status TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schemes (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
            department TEXT NOT NULL, summary TEXT NOT NULL, description TEXT NOT NULL,
            target_beneficiaries TEXT NOT NULL, benefits TEXT NOT NULL,
            eligibility_rules TEXT NOT NULL, required_documents TEXT NOT NULL,
            application_steps TEXT NOT NULL, important_notes TEXT NOT NULL,
            source_document_ids TEXT NOT NULL, demo_only INTEGER NOT NULL DEFAULT 1,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bookmarks (
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            scheme_id TEXT NOT NULL REFERENCES schemes(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL, PRIMARY KEY(user_id, scheme_id)
        );
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY, owner_user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL, original_filename TEXT NOT NULL, storage_path TEXT,
            mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, document_kind TEXT NOT NULL,
            source_name TEXT NOT NULL, source_url TEXT, is_sample INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'uploaded', processing_status TEXT NOT NULL DEFAULT 'pending',
            content_text TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1, uploaded_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            error TEXT, extracted_fields_json TEXT NOT NULL DEFAULT '{}',
            confirmed_fields_json TEXT NOT NULL DEFAULT '{}',
            issuing_authority TEXT NOT NULL DEFAULT '', document_type TEXT NOT NULL DEFAULT '',
            publication_date TEXT
        );
        CREATE TABLE IF NOT EXISTS document_chunks (
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL, page_number INTEGER, text TEXT NOT NULL,
            content_hash TEXT NOT NULL, created_at TEXT NOT NULL, section TEXT,
            UNIQUE(document_id, chunk_index, content_hash)
        );
        CREATE TABLE IF NOT EXISTS document_versions (
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            version INTEGER NOT NULL, content_hash TEXT NOT NULL, storage_path TEXT,
            created_at TEXT NOT NULL, UNIQUE(document_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_document ON document_chunks(document_id);
        CREATE TABLE IF NOT EXISTS processing_jobs (
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            job_type TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT, completed_at TEXT,
            error TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS eligibility_checks (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            scheme_id TEXT NOT NULL REFERENCES schemes(id), answers_json TEXT NOT NULL,
            result TEXT NOT NULL, conditions_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'en', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
            citations_json TEXT NOT NULL DEFAULT '[]', feedback TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            scheme_id TEXT NOT NULL REFERENCES schemes(id), reference_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL, form_json TEXT NOT NULL, document_ids_json TEXT NOT NULL DEFAULT '[]',
            mock_receipt TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS application_events (
            id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
            status TEXT NOT NULL, label TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id TEXT PRIMARY KEY, actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT,
            details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_apps_owner ON applications(user_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_docs_owner ON documents(owner_user_id, uploaded_at);
        CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
        CREATE TABLE IF NOT EXISTS app_migrations (
            version TEXT PRIMARY KEY, applied_at TEXT NOT NULL
        );
        """)
        # Additive migrations preserve existing users, uploads, schemes, and drafts.
        for table, column, declaration in (
            ("documents", "issuing_authority", "TEXT NOT NULL DEFAULT ''"),
            ("documents", "document_type", "TEXT NOT NULL DEFAULT ''"),
            ("documents", "publication_date", "TEXT"),
            ("document_chunks", "section", "TEXT"),
            ("schemes", "is_active", "INTEGER NOT NULL DEFAULT 1"),
        ):
            existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    _remove_legacy_demo_content()
    if settings.admin_email and settings.admin_password:
        from app.core.security import hash_password
        with connection() as db:
            exists = db.execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (settings.admin_email,)).fetchone()
            if exists is None:
                db.execute("INSERT INTO users(id,email,display_name,password_hash,role,created_at) VALUES(?,?,?,?,?,?)", (
                    str(uuid4()), settings.admin_email.lower(), "CivicPilot Administrator", hash_password(settings.admin_password), "admin",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ))


def _remove_legacy_demo_content() -> None:
    """Remove seeded fiction from active retrieval/catalogues without orphaning user records."""
    version = "2026-11-remove-fictional-demo-content"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sample_paths: list[str] = []
    with connection() as db:
        if db.execute("SELECT 1 FROM app_migrations WHERE version=?", (version,)).fetchone():
            return

        sample_paths = [row[0] for row in db.execute(
            "SELECT storage_path FROM documents WHERE is_sample=1 AND storage_path IS NOT NULL"
        ).fetchall()]
        db.execute("DELETE FROM documents WHERE is_sample=1")

        demo_rows = db.execute("SELECT id FROM schemes WHERE demo_only=1").fetchall()
        for (scheme_id,) in demo_rows:
            references = sum(db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE scheme_id=?", (scheme_id,)
            ).fetchone()[0] for table in ("applications", "bookmarks", "eligibility_checks"))
            if references:
                # Retain the FK target for historical user records, but remove its fictional details.
                db.execute("""UPDATE schemes SET is_active=0,name='Archived demonstration scheme',
                    category='Archived',department='Archived',summary='Unavailable historical demonstration record.',
                    description='This demonstration record has been removed from active use.',
                    target_beneficiaries='[]',benefits='[]',eligibility_rules='{}',required_documents='[]',
                    application_steps='[]',important_notes='[]',source_document_ids='[]' WHERE id=?""", (scheme_id,))
            else:
                db.execute("DELETE FROM schemes WHERE id=?", (scheme_id,))
        db.execute("INSERT INTO app_migrations(version,applied_at) VALUES(?,?)", (version, now))

    sample_root = (Path(__file__).resolve().parents[2] / "sample_documents").resolve()
    for raw_path in sample_paths:
        try:
            candidate = Path(raw_path).resolve()
            if sample_root in candidate.parents and candidate.is_file():
                candidate.unlink()
        except OSError:
            # The database record and its retrieval chunks are already removed.
            continue


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
