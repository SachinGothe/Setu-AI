import json

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.database import connection, initialize_database
from app.core.security import current_user


router = APIRouter(prefix="/api/v1", tags=["schemes"])


def _scheme(row, bookmarked: bool = False) -> dict:
    data = dict(row)
    for field in ("target_beneficiaries", "benefits", "eligibility_rules", "required_documents", "application_steps", "important_notes", "source_document_ids"):
        data[field] = json.loads(data[field])
    data["demo_only"] = bool(data["demo_only"])
    data["bookmarked"] = bookmarked
    return data


@router.get("/schemes")
def list_schemes(
    q: str = Query(default="", max_length=100),
    category: str | None = Query(default=None, max_length=80),
    state: str | None = Query(default=None, max_length=80),
    beneficiary: str | None = Query(default=None, max_length=80),
    user: dict = Depends(current_user),
) -> dict:
    initialize_database()
    query = "SELECT s.*, CASE WHEN b.user_id IS NULL THEN 0 ELSE 1 END AS is_bookmarked FROM schemes s LEFT JOIN bookmarks b ON b.scheme_id=s.id AND b.user_id=? WHERE s.is_active=1 AND s.demo_only=0"
    params: list = [user["id"]]
    if q.strip():
        term = f"%{q.strip()}%"
        query += " AND (s.name LIKE ? OR s.summary LIKE ? OR s.description LIKE ? OR s.department LIKE ?)"
        params.extend([term] * 4)
    if category:
        query += " AND s.category=?"
        params.append(category)
    if state:
        query += " AND (s.eligibility_rules LIKE ? OR s.eligibility_rules NOT LIKE '%states%')"
        params.append(f'%"{state}"%')
    if beneficiary:
        query += " AND s.target_beneficiaries LIKE ?"
        params.append(f"%{beneficiary}%")
    query += " ORDER BY s.category,s.name"
    with connection() as db:
        rows = db.execute(query, params).fetchall()
        categories = [row[0] for row in db.execute("SELECT DISTINCT category FROM schemes WHERE is_active=1 AND demo_only=0 ORDER BY category")]
    return {"items": [_scheme(row, bool(row["is_bookmarked"])) for row in rows], "categories": categories, "notice": "Only active verified catalogue records are displayed."}


@router.get("/schemes/{scheme_id}")
def scheme_detail(scheme_id: str, user: dict = Depends(current_user)) -> dict:
    initialize_database()
    with connection() as db:
        row = db.execute("SELECT s.*, EXISTS(SELECT 1 FROM bookmarks b WHERE b.user_id=? AND b.scheme_id=s.id) AS is_bookmarked FROM schemes s WHERE s.id=? AND s.is_active=1 AND s.demo_only=0", (user["id"], scheme_id)).fetchone()
        related = db.execute("SELECT * FROM schemes WHERE is_active=1 AND demo_only=0 AND category=(SELECT category FROM schemes WHERE id=?) AND id<>? ORDER BY name LIMIT 3", (scheme_id, scheme_id)).fetchall()
        docs = db.execute("SELECT id,title,source_name,0 AS is_sample FROM documents WHERE id IN (SELECT value FROM json_each((SELECT source_document_ids FROM schemes WHERE id=?))) AND is_sample=0", (scheme_id,)).fetchall()
    if row is None:
        raise HTTPException(status_code=404, detail="Sample scheme not found.")
    return {"scheme": _scheme(row, bool(row["is_bookmarked"])), "related": [_scheme(item) for item in related], "documents": [dict(doc) for doc in docs]}


@router.post("/schemes/{scheme_id}/bookmark")
def add_bookmark(scheme_id: str, user: dict = Depends(current_user)) -> dict:
    initialize_database()
    with connection() as db:
        if db.execute("SELECT 1 FROM schemes WHERE id=? AND is_active=1 AND demo_only=0", (scheme_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="No active verified scheme matches this identifier.")
        db.execute("INSERT OR IGNORE INTO bookmarks(user_id,scheme_id,created_at) VALUES(?,?,datetime('now'))", (user["id"], scheme_id))
    return {"scheme_id": scheme_id, "bookmarked": True}


@router.delete("/schemes/{scheme_id}/bookmark")
def remove_bookmark(scheme_id: str, user: dict = Depends(current_user)) -> dict:
    with connection() as db:
        db.execute("DELETE FROM bookmarks WHERE user_id=? AND scheme_id=?", (user["id"], scheme_id))
    return {"scheme_id": scheme_id, "bookmarked": False}


@router.get("/dashboard/summary")
def dashboard_summary(user: dict = Depends(current_user)) -> dict:
    initialize_database()
    with connection() as db:
        scheme_count = db.execute("SELECT COUNT(*) FROM schemes WHERE demo_only=0 AND is_active=1").fetchone()[0]
        check_count = db.execute("SELECT COUNT(*) FROM eligibility_checks WHERE user_id=?", (user["id"],)).fetchone()[0]
        document_count = db.execute("SELECT COUNT(*) FROM documents WHERE owner_user_id=? AND is_sample=0", (user["id"],)).fetchone()[0]
        app_rows = db.execute("SELECT status,COUNT(*) AS count FROM applications WHERE user_id=? GROUP BY status", (user["id"],)).fetchall()
        recent_apps = db.execute("SELECT a.id,a.reference_id,a.status,a.updated_at,s.name AS scheme_name FROM applications a JOIN schemes s ON s.id=a.scheme_id WHERE a.user_id=? ORDER BY a.updated_at DESC LIMIT 5", (user["id"],)).fetchall()
        bookmarked = db.execute("SELECT COUNT(*) FROM bookmarks WHERE user_id=?", (user["id"],)).fetchone()[0]
    return {
        "sample_scheme_count": scheme_count,
        "eligibility_checks": check_count,
        "uploaded_documents": document_count,
        "application_drafts": sum(row["count"] for row in app_rows if row["status"] in {"draft", "needs_information", "ready_for_review"}),
        "bookmarks": bookmarked,
        "application_statuses": {row["status"]: row["count"] for row in app_rows},
        "recent_applications": [dict(row) for row in recent_apps],
        "demo_only": True,
    }
