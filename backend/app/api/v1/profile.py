from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.database import connection, initialize_database
from app.core.security import current_user
from app.services.audit import record_audit


router = APIRouter(prefix="/api/v1", tags=["profile"])


class ProfilePayload(BaseModel):
    age: int | None = Field(default=None, ge=0, le=120)
    state: str | None = Field(default=None, max_length=80)
    annual_family_income: float | None = Field(default=None, ge=0, le=100000000)
    occupation: str | None = Field(default=None, max_length=100)
    education_level: str | None = Field(default=None, max_length=60)
    employment_status: str | None = Field(default=None, max_length=60)


@router.get("/profile")
def get_profile(user: dict = Depends(current_user)) -> dict:
    initialize_database()
    with connection() as db:
        row = db.execute("SELECT age,state,annual_family_income,occupation,education_level,employment_status,updated_at FROM user_profiles WHERE user_id=?", (user["id"],)).fetchone()
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "role": user["role"],
        **(dict(row) if row else {}),
    }


@router.put("/profile")
def update_profile(payload: ProfilePayload, user: dict = Depends(current_user)) -> dict:
    initialize_database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data = payload.model_dump()
    with connection() as db:
        db.execute("""INSERT INTO user_profiles(user_id,age,state,annual_family_income,occupation,education_level,employment_status,updated_at)
            VALUES(:user_id,:age,:state,:annual_family_income,:occupation,:education_level,:employment_status,:updated_at)
            ON CONFLICT(user_id) DO UPDATE SET age=excluded.age,state=excluded.state,annual_family_income=excluded.annual_family_income,
            occupation=excluded.occupation,education_level=excluded.education_level,employment_status=excluded.employment_status,updated_at=excluded.updated_at""",
            {"user_id": user["id"], **data, "updated_at": now})
    record_audit(user["id"], "profile_updated", "profile", user["id"])
    return {"id": user["id"], "email": user["email"], "display_name": user["display_name"], "role": user["role"], **data, "updated_at": now}
