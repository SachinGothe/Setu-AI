from pathlib import Path
from datetime import date
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.core.rate_limit import enforce_rate_limit
from app.core.security import admin_user, current_user
from app.services import document_service
from app.services.audit import record_audit


router = APIRouter(prefix="/api/v1", tags=["documents"])


class KnowledgeMetadataPayload(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    issuing_authority: str = Field(min_length=2, max_length=160)
    document_type: str = Field(min_length=2, max_length=80)
    publication_date: str | None = Field(default=None, max_length=10)
    source_url: str | None = Field(default=None, max_length=500)


class CitationLocationPayload(BaseModel):
    page_number: int | None = Field(default=None, ge=1, le=10000)
    section: str | None = Field(default=None, max_length=160)


async def _read_upload(file: UploadFile) -> bytes:
    limit = document_service.get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"File exceeds the {limit // (1024 * 1024)} MB upload limit.")
    return data


def _create(**args) -> dict:
    try:
        return document_service.create_document(**args)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_source_reference(value: str | None) -> None:
    if not value:
        return
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Source URL is malformed.") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or port not in (None, 443):
        raise HTTPException(status_code=422, detail="Source URL must be HTTPS without credentials or custom ports. It is metadata only; no network request is made.")


@router.get("/documents")
def list_documents(user: dict = Depends(current_user)) -> dict:
    return {"items": document_service.list_documents(user["id"]), "notice": "Sample files are demonstration data. Private uploads are visible only to their owner."}


@router.post("/documents", status_code=201)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None, max_length=160),
    document_kind: str = Form(default="supporting_document", max_length=80),
    user: dict = Depends(current_user),
) -> dict:
    enforce_rate_limit(request, "document_upload", limit=20, window_seconds=3600)
    data = await _read_upload(file)
    doc = _create(owner_user_id=user["id"], filename=file.filename or "upload", content_type=file.content_type or "", data=data, title=title, document_kind=document_kind)
    record_audit(user["id"], "document_uploaded", "document", doc["id"], {"mime_type": file.content_type, "size_bytes": len(data)})
    return doc


@router.get("/documents/{document_id}")
def get_document(document_id: str, user: dict = Depends(current_user)) -> dict:
    doc = document_service.get_document(document_id)
    if doc is None or (not doc["is_sample"] and doc["owner_user_id"] != user["id"] and doc["document_kind"] != "knowledge"):
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"document": doc, "extracted_text": document_service.document_content(document_id, user["id"], allow_knowledge=True)}


@router.get("/documents/{document_id}/download")
def download_document(document_id: str, user: dict = Depends(current_user)):
    info = document_service.document_file(document_id, user["id"])
    if not info:
        raise HTTPException(status_code=404, detail="Document file not found.")
    path, filename, mime = info
    return FileResponse(path, media_type=mime, filename=Path(filename).name)


@router.patch("/documents/{document_id}/review")
def review_document(document_id: str, payload: dict, user: dict = Depends(current_user)) -> dict:
    doc = document_service.get_document(document_id)
    if doc is None or doc["is_sample"] or doc["owner_user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Private document not found.")
    fields = payload.get("fields")
    if not isinstance(fields, dict) or len(fields) > 30:
        raise HTTPException(status_code=422, detail="Provide a fields object with at most 30 reviewed values.")
    allowed = {"name", "date_of_birth", "address", "document_number", "issue_date", "expiry_date"}
    if set(fields) - allowed or any(not isinstance(value, str) or len(value) > 300 for value in fields.values()):
        raise HTTPException(status_code=422, detail="Review fields must contain supported text values only.")
    import json
    from datetime import datetime, timezone
    with document_service.connection() as db:
        db.execute("UPDATE documents SET confirmed_fields_json=?,status='reviewed',updated_at=? WHERE id=? AND owner_user_id=?", (
            json.dumps({key: {"value": value, "confirmed": True} for key, value in fields.items()}, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(timespec="seconds"), document_id, user["id"],
        ))
    record_audit(user["id"], "document_fields_reviewed", "document", document_id)
    return document_service.get_document(document_id)


@router.post("/documents/{document_id}/reprocess")
def reprocess_document(document_id: str, user: dict = Depends(current_user)) -> dict:
    doc = document_service.get_document(document_id)
    if doc is None or doc["is_sample"] or doc["owner_user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Private document not found.")
    try:
        result = document_service.reprocess_document(document_id)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record_audit(user["id"], "document_reprocessed", "document", document_id)
    return result


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str, user: dict = Depends(current_user)):
    doc = document_service.get_document(document_id)
    if doc is None or doc["is_sample"] or doc["owner_user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Private document not found.")
    with document_service.connection() as db:
        row = db.execute("SELECT storage_path FROM documents WHERE id=? AND owner_user_id=?", (document_id, user["id"])).fetchone()
        db.execute("DELETE FROM documents WHERE id=? AND owner_user_id=?", (document_id, user["id"]))
    if row and row["storage_path"]:
        path = Path(row["storage_path"]).resolve()
        if document_service.storage_root() in path.parents and path.is_file():
            path.unlink()
    record_audit(user["id"], "document_deleted", "document", document_id)
    return None


@router.get("/admin/knowledge-documents")
def list_knowledge_documents(admin: dict = Depends(admin_user)) -> dict:
    with document_service.connection() as db:
        rows = db.execute("SELECT id,title,original_filename,mime_type,size_bytes,source_name,source_url,issuing_authority,document_type,publication_date,status,processing_status,version,uploaded_at,updated_at,error,is_sample FROM documents WHERE document_kind='knowledge' AND is_sample=0 ORDER BY uploaded_at DESC").fetchall()
    return {"items": [dict(row) for row in rows]}


@router.get("/admin/knowledge-documents/{document_id}")
def get_knowledge_document(document_id: str, admin: dict = Depends(admin_user)) -> dict:
    doc = document_service.get_document(document_id)
    if doc is None or (doc["document_kind"] != "knowledge" and not doc["is_sample"]):
        raise HTTPException(status_code=404, detail="Knowledge document not found.")
    with document_service.connection() as db:
        chunks = db.execute("SELECT id,page_number,section,text FROM document_chunks WHERE document_id=? ORDER BY chunk_index", (document_id,)).fetchall()
    return {"document": doc, "pages": [dict(row) for row in chunks]}


@router.patch("/admin/knowledge-documents/{document_id}")
def update_knowledge_metadata(document_id: str, payload: KnowledgeMetadataPayload, admin: dict = Depends(admin_user)) -> dict:
    _validate_source_reference(payload.source_url)
    if payload.publication_date:
        try:
            date.fromisoformat(payload.publication_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Publication date must use YYYY-MM-DD format.") from exc
    with document_service.connection() as db:
        result = db.execute("UPDATE documents SET title=?,source_name=?,issuing_authority=?,document_type=?,publication_date=?,source_url=?,updated_at=datetime('now') WHERE id=? AND document_kind='knowledge' AND is_sample=0", (
            payload.title.strip(), payload.issuing_authority.strip(), payload.issuing_authority.strip(), payload.document_type.strip(), payload.publication_date, payload.source_url, document_id,
        ))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Knowledge document not found.")
    record_audit(admin["id"], "knowledge_document_metadata_updated", "document", document_id)
    return document_service.get_document(document_id)


@router.patch("/admin/knowledge-documents/{document_id}/passages/{chunk_id}")
def update_citation_location(document_id: str, chunk_id: str, payload: CitationLocationPayload, admin: dict = Depends(admin_user)) -> dict:
    """Correct citation location only; passage text remains unchanged from extraction."""
    with document_service.connection() as db:
        result = db.execute("UPDATE document_chunks SET page_number=?,section=? WHERE id=? AND document_id=? AND EXISTS (SELECT 1 FROM documents d WHERE d.id=? AND d.document_kind='knowledge' AND d.is_sample=0)", (
            payload.page_number, payload.section.strip() if payload.section else None, chunk_id, document_id, document_id,
        ))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Indexed passage not found.")
        row = db.execute("SELECT page_number,section,text FROM document_chunks WHERE id=?", (chunk_id,)).fetchone()
    record_audit(admin["id"], "citation_location_updated", "document_chunk", chunk_id)
    return dict(row)


@router.get("/admin/knowledge-documents/{document_id}/download")
def download_knowledge_document(document_id: str, admin: dict = Depends(admin_user)):
    info = document_service.document_file(document_id, None, allow_knowledge=True)
    if not info:
        raise HTTPException(status_code=404, detail="Knowledge document file not found.")
    path, filename, mime = info
    return FileResponse(path, media_type=mime, filename=Path(filename).name)


@router.post("/admin/knowledge-documents", status_code=201)
async def upload_knowledge_document(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None, max_length=160),
    source_name: str = Form(min_length=2, max_length=160),
    source_url: str | None = Form(default=None, max_length=500),
    issuing_authority: str | None = Form(default=None, max_length=160),
    document_type: str = Form(default="legal_reference", min_length=2, max_length=80),
    publication_date: str | None = Form(default=None, max_length=10),
    admin: dict = Depends(admin_user),
) -> dict:
    enforce_rate_limit(request, "knowledge_upload", limit=40, window_seconds=3600)
    _validate_source_reference(source_url)
    if publication_date:
        try:
            date.fromisoformat(publication_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Publication date must use YYYY-MM-DD format.") from exc
    data = await _read_upload(file)
    authority = (issuing_authority or source_name).strip()
    doc = _create(owner_user_id=None, filename=file.filename or "knowledge", content_type=file.content_type or "", data=data, title=title, document_kind="knowledge", source_name=authority, source_url=source_url, knowledge=True, issuing_authority=authority, document_type=document_type.strip(), publication_date=publication_date)
    record_audit(admin["id"], "knowledge_document_uploaded", "document", doc["id"], {"issuing_authority": authority, "document_type": document_type})
    return doc


@router.post("/admin/knowledge-documents/{document_id}/reprocess")
def reprocess_knowledge_document(document_id: str, admin: dict = Depends(admin_user)) -> dict:
    doc = document_service.get_document(document_id)
    if doc is None or doc["document_kind"] != "knowledge" or doc["is_sample"]:
        raise HTTPException(status_code=404, detail="Knowledge document not found.")
    result = document_service.reprocess_document(document_id)
    record_audit(admin["id"], "knowledge_document_reprocessed", "document", document_id)
    return result


@router.delete("/admin/knowledge-documents/{document_id}", status_code=204)
def delete_knowledge_document(document_id: str, admin: dict = Depends(admin_user)):
    doc = document_service.get_document(document_id)
    if doc is None or doc["document_kind"] != "knowledge" or doc["is_sample"]:
        raise HTTPException(status_code=404, detail="Knowledge document not found.")
    with document_service.connection() as db:
        row = db.execute("SELECT storage_path FROM documents WHERE id=?", (document_id,)).fetchone()
        db.execute("DELETE FROM documents WHERE id=?", (document_id,))
    if row and row["storage_path"]:
        path = Path(row["storage_path"]).resolve()
        if document_service.storage_root() in path.parents and path.is_file():
            path.unlink()
    record_audit(admin["id"], "knowledge_document_deleted", "document", document_id)
    return None


@router.get("/admin/processing-jobs")
def processing_jobs(admin: dict = Depends(admin_user)) -> dict:
    with document_service.connection() as db:
        rows = db.execute("SELECT j.*,d.title FROM processing_jobs j JOIN documents d ON d.id=j.document_id ORDER BY j.created_at DESC LIMIT 200").fetchall()
    return {"items": [dict(row) for row in rows]}
