import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.database import connection, initialize_database
from app.core.security import current_user
from app.services.audit import record_audit
from app.services.eligibility_engine import evaluate


router = APIRouter(prefix="/api/v1/eligibility", tags=["eligibility"])


class EligibilityPayload(BaseModel):
    scheme_id: str = Field(min_length=1, max_length=100)
    answers: dict = Field(default_factory=dict)


@router.post("/check")
def check_eligibility(payload: EligibilityPayload, user: dict = Depends(current_user)) -> dict:
    initialize_database()
    with connection() as db:
        scheme = db.execute("SELECT * FROM schemes WHERE id=? AND is_active=1 AND demo_only=0", (payload.scheme_id,)).fetchone()
        if scheme is None:
            raise HTTPException(status_code=404, detail="No active verified scheme matches this identifier.")
        profile = db.execute("SELECT age,state,annual_family_income,occupation,education_level,employment_status FROM user_profiles WHERE user_id=?", (user["id"],)).fetchone()
        docs = db.execute("SELECT id,title,source_name,source_url,is_sample FROM documents WHERE id IN (SELECT value FROM json_each(?))", (scheme["source_document_ids"],)).fetchall()
    answers = dict(profile) if profile else {}
    answers.update(payload.answers)
    for field in ("age", "annual_family_income"):
        value = answers.get(field)
        if value is not None:
            try:
                answers[field] = int(value) if field == "age" else float(value)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=422, detail=f"{field} must be numeric.") from exc
    citations = [{"document_id": doc["id"], "title": doc["title"], "page": None, "source_name": doc["source_name"], "source_url": doc["source_url"], "demo_only": False} for doc in docs if not doc["is_sample"]]
    result = evaluate(json.loads(scheme["eligibility_rules"]), answers, citations)
    check_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connection() as db:
        db.execute("INSERT INTO eligibility_checks(id,user_id,scheme_id,answers_json,result,conditions_json,created_at) VALUES(?,?,?,?,?,?,?)", (
            check_id, user["id"], payload.scheme_id, json.dumps(answers, ensure_ascii=False), result["result"], json.dumps(result, ensure_ascii=False), now,
        ))
    record_audit(user["id"], "eligibility_checked", "scheme", payload.scheme_id, {"result": result["result"]})
    return {"id": check_id, "scheme": {"id": scheme["id"], "name": scheme["name"]}, "answers": answers, "created_at": now, **result}


@router.get("/history")
def eligibility_history(user: dict = Depends(current_user)) -> dict:
    initialize_database()
    with connection() as db:
        rows = db.execute("SELECT e.id,e.scheme_id,e.result,e.created_at,s.name FROM eligibility_checks e JOIN schemes s ON s.id=e.scheme_id WHERE e.user_id=? ORDER BY e.created_at DESC LIMIT 100", (user["id"],)).fetchall()
    return {"items": [dict(row) for row in rows]}
