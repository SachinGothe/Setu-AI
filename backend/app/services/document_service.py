"""Private local upload storage, processing, versioning, and ownership-aware helpers."""

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings
from app.core.database import connection, initialize_database
from app.services.document_processing import ExtractionUnavailable, chunk_text, content_sha256, extract_html, extract_image, extract_pdf_with_ocr, extract_plain_text


ALLOWED = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".html": "text/html",
    ".txt": "text/plain",
}
USER_ALLOWED = {"application/pdf", "image/jpeg", "image/png"}
_SECTION = re.compile(r"^(?:(?i:chapter|section|part|article|schedule|annex(?:ure)?|rule|regulation)\s+[A-Z0-9IVX.-]+.*|[A-Z][A-Z0-9 ,:&()/-]{5,100})$")


def storage_root() -> Path:
    configured = Path(get_settings().upload_storage_path)
    root = configured if configured.is_absolute() else Path(__file__).resolve().parents[2] / configured
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _detect_section(chunk: str) -> str | None:
    for line in chunk.splitlines():
        candidate = line.strip().rstrip(":")
        if 5 <= len(candidate) <= 120 and _SECTION.fullmatch(candidate):
            return candidate
    return None


def validate_upload(filename: str, content_type: str, data: bytes, *, knowledge: bool = False) -> tuple[str, str]:
    extension = Path(filename or "").suffix.lower()
    allowed_mimes = set(ALLOWED.values()) if knowledge else USER_ALLOWED
    expected = ALLOWED.get(extension)
    if expected is None or content_type not in allowed_mimes or expected != content_type:
        raise ValueError("Upload a PDF, JPG, JPEG, PNG" + (", or HTML knowledge file." if knowledge else " file."))
    if not data:
        raise ValueError("The uploaded file is empty.")
    if content_type == "application/pdf" and not data.startswith(b"%PDF-"):
        raise ValueError("The file contents do not match a PDF.")
    if content_type == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("The file contents do not match a PNG image.")
    if content_type == "image/jpeg" and not data.startswith(b"\xff\xd8\xff"):
        raise ValueError("The file contents do not match a JPEG image.")
    if content_type == "text/html" and b"<" not in data[:1024]:
        raise ValueError("The file contents do not look like HTML.")
    safe_name = Path(filename).name
    safe_name = re.sub(r"[\x00-\x1f\x7f]", "", safe_name)[:180]
    return extension, safe_name or f"upload{extension}"


def _extract(data: bytes, mime: str) -> tuple[list[tuple[int | None, str]], str]:
    if mime == "application/pdf":
        return extract_pdf_with_ocr(data)
    if mime in {"image/jpeg", "image/png"}:
        try:
            pages = extract_image(data)
            return pages, "ocr_used" if pages and pages[0][1] else "ocr_empty"
        except ExtractionUnavailable:
            return [], "ocr_unavailable"
    if mime == "text/html":
        pages = extract_html(data)
        return pages, "text_extracted" if pages else "empty_text"
    if mime == "text/plain":
        pages = extract_plain_text(data)
        return pages, "text_extracted" if pages else "empty_text"
    return [], "unsupported"


def _suggest_fields(text: str) -> dict:
    patterns = {
        "name": r"(?im)^\s*name\s*[:\-]\s*([^\n]{2,100})",
        "date_of_birth": r"(?im)^\s*(?:date of birth|dob)\s*[:\-]\s*([^\n]{2,40})",
        "address": r"(?im)^\s*address\s*[:\-]\s*([^\n]{3,180})",
        "document_number": r"(?im)^\s*(?:document number|certificate number|id number)\s*[:\-]\s*([^\n]{2,60})",
        "issue_date": r"(?im)^\s*issue date\s*[:\-]\s*([^\n]{2,40})",
        "expiry_date": r"(?im)^\s*(?:expiry date|expires)\s*[:\-]\s*([^\n]{2,40})",
    }
    fields = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            fields[key] = {"value": match.group(1).strip(), "confirmed": False}
    return fields


def create_document(
    *, owner_user_id: str | None, filename: str, content_type: str, data: bytes,
    title: str | None = None, document_kind: str = "supporting_document", source_name: str = "User upload",
    source_url: str | None = None, knowledge: bool = False, issuing_authority: str = "",
    document_type: str = "", publication_date: str | None = None,
) -> dict:
    initialize_database()
    if len(data) > get_settings().max_upload_mb * 1024 * 1024:
        raise ValueError(f"File exceeds the {get_settings().max_upload_mb} MB upload limit.")
    extension, safe_name = validate_upload(filename, content_type, data, knowledge=knowledge)
    digest = content_sha256(data)
    now = _now()
    with connection() as db:
        duplicate = db.execute("SELECT id,title,version FROM documents WHERE owner_user_id IS ? AND content_hash=? AND is_sample=0", (owner_user_id, digest)).fetchone()
        if duplicate:
            return {"id": duplicate["id"], "title": duplicate["title"], "version": duplicate["version"], "duplicate": True, "processing_status": "duplicate"}

    root = storage_root()
    storage_name = secrets.token_hex(24) + extension
    storage_path = (root / storage_name).resolve()
    if root not in storage_path.parents:
        raise ValueError("Invalid upload storage path.")
    descriptor = os.open(storage_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(data)

    document_id = str(uuid4())
    job_id = str(uuid4())
    document_title = (title or Path(safe_name).stem).strip()[:160] or "Uploaded document"
    with connection() as db:
        same_kind = db.execute("SELECT MAX(version) FROM documents WHERE owner_user_id IS ? AND document_kind=? AND title=? AND is_sample=0", (owner_user_id, document_kind, document_title)).fetchone()[0]
        version = int(same_kind or 0) + 1
        db.execute("INSERT INTO documents(id,owner_user_id,title,original_filename,storage_path,mime_type,size_bytes,document_kind,source_name,source_url,is_sample,status,processing_status,content_hash,version,uploaded_at,updated_at,issuing_authority,document_type,publication_date) VALUES(?,?,?,?,?,?,?,?,?,?,0,'uploaded','processing',?,?,?,?,?,?,?)", (
            document_id, owner_user_id, document_title, safe_name, str(storage_path), content_type, len(data), document_kind, source_name, source_url, digest, version, now, now,
            issuing_authority or source_name, document_type or document_kind, publication_date,
        ))
        db.execute("INSERT INTO document_versions(id,document_id,version,content_hash,storage_path,created_at) VALUES(?,?,?,?,?,?)", (str(uuid4()), document_id, version, digest, str(storage_path), now))
        db.execute("INSERT INTO processing_jobs(id,document_id,job_type,status,started_at,created_at) VALUES(?,?,?,'running',?,?)", (job_id, document_id, "extract_and_index", now, now))

    try:
        pages, extraction_status = _extract(data, content_type)
        content = "\n\n".join(text for _, text in pages)
        suggestions = _suggest_fields(content)
        with connection() as db:
            db.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
            index = 0
            for page_number, page_text in pages:
                page_section = _detect_section(page_text)
                for chunk in chunk_text(page_text, size=180, overlap=25):
                    db.execute("INSERT INTO document_chunks(id,document_id,chunk_index,page_number,text,content_hash,created_at,section) VALUES(?,?,?,?,?,?,?,?)", (
                        str(uuid4()), document_id, index, page_number, chunk, content_sha256(chunk.encode()), _now(), page_section or _detect_section(chunk),
                    ))
                    index += 1
            db.execute("UPDATE documents SET status=?,processing_status=?,content_text=?,extracted_fields_json=?,error=?,updated_at=? WHERE id=?", (
                "review_required" if content or extraction_status in {"ocr_unavailable", "ocr_empty"} else "extraction_failed",
                extraction_status, content, json.dumps(suggestions, ensure_ascii=False),
                "OCR could not run; install/configure Tesseract to process scanned images." if extraction_status == "ocr_unavailable" else None,
                _now(), document_id,
            ))
            db.execute("UPDATE processing_jobs SET status=?,completed_at=?,error=? WHERE id=?", (
                "complete" if extraction_status not in {"ocr_unavailable", "empty_text", "ocr_empty"} else "needs_action",
                _now(), "OCR or readable text is unavailable." if extraction_status in {"ocr_unavailable", "empty_text", "ocr_empty"} else None, job_id,
            ))
    except (ValueError, ExtractionUnavailable) as exc:
        with connection() as db:
            db.execute("UPDATE documents SET status='processing_failed',processing_status='failed',error=?,updated_at=? WHERE id=?", (str(exc)[:500], _now(), document_id))
            db.execute("UPDATE processing_jobs SET status='failed',completed_at=?,error=? WHERE id=?", (_now(), str(exc)[:500], job_id))
    return get_document(document_id)


def import_curated_reference_pdfs(admin_email: str, paths: dict[str, str]) -> list[dict]:
    """Import the three administrator-supplied PDFs; safe to rerun by file hash."""
    initialize_database()
    with connection() as db:
        admin = db.execute("SELECT id FROM users WHERE email=? COLLATE NOCASE AND role='admin' AND is_active=1", (admin_email.strip(),)).fetchone()
    if admin is None:
        raise ValueError("An active administrator account matching ADMIN_EMAIL must exist before importing references.")

    records = [
        ("pm_vishwakarma", "PM Vishwakarma Scheme: Honouring Heritage, Powering Progress", "Press Information Bureau, Government of India", "Government scheme information article", "2025-09-16", "pm vishwakarma.pdf"),
        ("pm_kisan", "Pradhan Mantri Kisan Samman Nidhi (PM-KISAN) Scheme – Operational Guidelines", "Ministry of Agriculture & Farmers' Welfare", "Scheme operational guidelines", "2020-03-29", "RevisedPM-KISANOperationalGuidelines(English).pdf"),
        ("scholarship", "Scholarship Eligibility Check", "National Scholarship Portal (uploaded eligibility document)", "Scholarship eligibility information", None, "Scholarship Eligibility Check.pdf"),
    ]
    imported = []
    for key, title, authority, doc_type, pub_date, filename in records:
        path = Path(paths[key]).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"The supplied {filename} file was not found at its configured path.")
        data = path.read_bytes()
        result = create_document(owner_user_id=None, filename=path.name, content_type="application/pdf", data=data,
            title=title, document_kind="knowledge", source_name=authority, source_url=None, knowledge=True,
            issuing_authority=authority, document_type=doc_type, publication_date=pub_date)
        if result.get("duplicate"):
            with connection() as db:
                db.execute("UPDATE documents SET title=?,source_name=?,issuing_authority=?,document_type=?,publication_date=?,updated_at=? WHERE id=? AND document_kind='knowledge' AND owner_user_id IS NULL", (
                    title, authority, authority, doc_type, pub_date, _now(), result["id"],
                ))
            result = get_document(result["id"])
        from app.services.audit import record_audit
        record_audit(admin["id"], "knowledge_document_imported", "document", result["id"], {"filename": path.name, "processing_status": result["processing_status"]})
        imported.append(result)
    return imported


def get_document(document_id: str) -> dict | None:
    initialize_database()
    with connection() as db:
        row = db.execute("SELECT id,owner_user_id,title,original_filename,mime_type,size_bytes,document_kind,source_name,source_url,issuing_authority,document_type,publication_date,is_sample,status,processing_status,content_hash,version,uploaded_at,updated_at,error,extracted_fields_json,confirmed_fields_json FROM documents WHERE id=?", (document_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["is_sample"] = bool(result["is_sample"])
    result["extracted_fields"] = json.loads(result.pop("extracted_fields_json"))
    result["confirmed_fields"] = json.loads(result.pop("confirmed_fields_json"))
    return result


def reprocess_document(document_id: str) -> dict:
    document = get_document(document_id)
    if document is None or document["is_sample"]:
        raise KeyError("Document not found or cannot be reprocessed.")
    file_info = document_file(document_id, document["owner_user_id"], allow_knowledge=document["document_kind"] == "knowledge")
    if not file_info:
        raise FileNotFoundError("The private source file is unavailable.")
    path, _, mime = file_info
    data = Path(path).read_bytes()
    now = _now()
    job_id = str(uuid4())
    with connection() as db:
        db.execute("UPDATE documents SET status='uploaded',processing_status='processing',error=NULL,updated_at=? WHERE id=?", (now, document_id))
        db.execute("INSERT INTO processing_jobs(id,document_id,job_type,status,started_at,created_at) VALUES(?,?,?,'running',?,?)", (job_id, document_id, "reprocess", now, now))
    try:
        pages, processing_state = _extract(data, mime)
        content = "\n\n".join(text for _, text in pages)
        suggestions = _suggest_fields(content)
        with connection() as db:
            db.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
            index = 0
            for page_number, page_text in pages:
                page_section = _detect_section(page_text)
                for chunk in chunk_text(page_text, size=180, overlap=25):
                    db.execute("INSERT INTO document_chunks(id,document_id,chunk_index,page_number,text,content_hash,created_at,section) VALUES(?,?,?,?,?,?,?,?)", (
                        str(uuid4()), document_id, index, page_number, chunk, content_sha256(chunk.encode()), _now(), page_section or _detect_section(chunk),
                    ))
                    index += 1
            db.execute("UPDATE documents SET content_text=?,extracted_fields_json=?,processing_status=?,status=?,error=?,updated_at=? WHERE id=?", (
                content, json.dumps(suggestions, ensure_ascii=False), processing_state,
                "review_required" if content else "extraction_failed",
                "OCR could not run; install/configure Tesseract to process scanned images." if processing_state == "ocr_unavailable" else None,
                _now(), document_id,
            ))
            db.execute("UPDATE processing_jobs SET status=?,completed_at=?,error=? WHERE id=?", (
                "complete" if processing_state not in {"ocr_unavailable", "empty_text", "ocr_empty"} else "needs_action",
                _now(), None if content else "No readable text was extracted.", job_id,
            ))
    except (ValueError, ExtractionUnavailable) as exc:
        with connection() as db:
            db.execute("UPDATE documents SET status='processing_failed',processing_status='failed',error=?,updated_at=? WHERE id=?", (str(exc)[:500], _now(), document_id))
            db.execute("UPDATE processing_jobs SET status='failed',completed_at=?,error=? WHERE id=?", (_now(), str(exc)[:500], job_id))
    return get_document(document_id)


def list_documents(owner_id: str) -> list[dict]:
    initialize_database()
    with connection() as db:
        rows = db.execute("SELECT id FROM documents WHERE (owner_user_id=? OR document_kind='knowledge') AND is_sample=0 ORDER BY uploaded_at DESC", (owner_id,)).fetchall()
    return [get_document(row["id"]) for row in rows]


def document_content(document_id: str, owner_id: str, *, allow_knowledge: bool = True) -> str | None:
    initialize_database()
    with connection() as db:
        visibility = "owner_user_id=? AND is_sample=0" + (" OR (document_kind='knowledge' AND is_sample=0)" if allow_knowledge else "")
        row = db.execute(f"SELECT content_text FROM documents WHERE id=? AND ({visibility})", (document_id, owner_id)).fetchone()
    return row["content_text"] if row else None


def document_file(document_id: str, owner_id: str | None, *, allow_knowledge: bool = False) -> tuple[str, str, str] | None:
    initialize_database()
    with connection() as db:
        visibility = "owner_user_id=? AND is_sample=0" + (" OR (document_kind='knowledge' AND is_sample=0)" if allow_knowledge else "")
        row = db.execute(f"SELECT storage_path,original_filename,mime_type FROM documents WHERE id=? AND ({visibility})", (document_id, owner_id)).fetchone()
    if not row or not row["storage_path"]:
        return None
    path = Path(row["storage_path"]).resolve()
    if not path.is_file():
        return None
    if storage_root() not in path.parents:
        return None
    return str(path), row["original_filename"], row["mime_type"]
