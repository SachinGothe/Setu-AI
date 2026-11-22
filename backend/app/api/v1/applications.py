"""Application preparation with a strictly local, clearly simulated mock portal."""
import json
import secrets
from datetime import datetime,timezone
from uuid import uuid4

from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from app.core.database import connection
from app.core.security import current_user
from app.services.audit import record_audit

router=APIRouter(prefix="/api/v1",tags=["applications"])
class DraftPayload(BaseModel):
    scheme_id:str=Field(min_length=1,max_length=100)
    form:dict
    document_ids:list[str]=Field(default_factory=list,max_length=20)

def _record(row):
    data=dict(row);data["form"]=json.loads(data.pop("form_json"));data["document_ids"]=json.loads(data.pop("document_ids_json"));return data

@router.get("/applications")
def list_apps(user:dict=Depends(current_user)):
    with connection() as db: rows=db.execute("SELECT a.*,s.name AS scheme_name FROM applications a JOIN schemes s ON s.id=a.scheme_id WHERE a.user_id=? ORDER BY a.updated_at DESC",(user["id"],)).fetchall()
    return {"items":[_record(r) for r in rows],"notice":"Drafts and tracking events are local demonstrations. Nothing is submitted to a government service."}

@router.post("/applications",status_code=201)
def create_draft(payload:DraftPayload,user:dict=Depends(current_user)):
    if len(json.dumps(payload.form,ensure_ascii=False))>20000: raise HTTPException(422,"Form data is too large.")
    now=datetime.now(timezone.utc).isoformat(timespec="seconds");app_id=str(uuid4());ref="DEMO-"+secrets.token_hex(4).upper()
    with connection() as db:
        scheme=db.execute("SELECT id,required_documents FROM schemes WHERE id=? AND is_active=1 AND demo_only=0",(payload.scheme_id,)).fetchone()
        if not scheme: raise HTTPException(404,"No active verified scheme matches this identifier.")
        for doc_id in payload.document_ids:
            if not db.execute("SELECT id FROM documents WHERE id=? AND owner_user_id=? AND is_sample=0",(doc_id,user["id"])).fetchone(): raise HTTPException(422,"A selected document is not available to your account.")
        missing=[str(x) for x in json.loads(scheme["required_documents"]) if x.lower().replace(" (sample)","").replace(" ","_") not in payload.form]
        status="needs_information" if missing else "ready_for_review"
        db.execute("INSERT INTO applications(id,user_id,scheme_id,reference_id,status,form_json,document_ids_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(app_id,user["id"],payload.scheme_id,ref,status,json.dumps(payload.form,ensure_ascii=False),json.dumps(payload.document_ids),now,now))
        db.execute("INSERT INTO application_events(id,application_id,status,label,details,created_at) VALUES(?,?,?,?,?,?)",(str(uuid4()),app_id,status,"Draft created","Local preparation only; no external submission.",now))
    record_audit(user["id"],"application_draft_created","application",app_id)
    return {"id":app_id,"reference_id":ref,"scheme_id":payload.scheme_id,"status":status,"form":payload.form,"document_ids":payload.document_ids,"missing_fields":missing,"created_at":now,"updated_at":now,"demo_only":True}

@router.get("/applications/{application_id}")
def get_app(application_id:str,user:dict=Depends(current_user)):
    with connection() as db:
        row=db.execute("SELECT a.*,s.name AS scheme_name,s.required_documents FROM applications a JOIN schemes s ON s.id=a.scheme_id WHERE a.id=? AND a.user_id=?",(application_id,user["id"])).fetchone()
        events=db.execute("SELECT status,label,details,created_at FROM application_events WHERE application_id=? ORDER BY created_at",(application_id,)).fetchall()
    if not row: raise HTTPException(404,"Application draft not found.")
    data=_record(row)
    required=json.loads(data.pop("required_documents"))
    data["required_fields"]=[x.lower().replace(" (sample)","").replace(" ","_") for x in required]
    data["missing_fields"]=[field for field in data["required_fields"] if field not in data["form"] or data["form"][field] in (None,"")]
    data["events"]=[dict(e) for e in events];return data

@router.put("/applications/{application_id}")
def update_app(application_id:str,payload:DraftPayload,user:dict=Depends(current_user)):
    if len(json.dumps(payload.form,ensure_ascii=False))>20000: raise HTTPException(422,"Form data is too large.")
    now=datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connection() as db:
        row=db.execute("SELECT id FROM applications WHERE id=? AND user_id=? AND scheme_id=?",(application_id,user["id"],payload.scheme_id)).fetchone()
        if not row: raise HTTPException(404,"Application draft not found.")
        db.execute("UPDATE applications SET form_json=?,document_ids_json=?,updated_at=? WHERE id=?",(json.dumps(payload.form,ensure_ascii=False),json.dumps(payload.document_ids),now,application_id))
    return get_app(application_id,user)

@router.post("/applications/{application_id}/mock-prepare")
def mock_prepare(application_id:str,user:dict=Depends(current_user)):
    app=get_app(application_id,user)
    now=datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connection() as db:
        db.execute("UPDATE applications SET status='mock_ready',updated_at=? WHERE id=?",(now,application_id))
        db.execute("INSERT INTO application_events(id,application_id,status,label,details,created_at) VALUES(?,?,?,?,?,?)",(str(uuid4()),application_id,"mock_ready","Prepared in local mock portal","No government site was opened and no application was submitted.",now))
    return {"status":"mock_ready","application_id":application_id,"review":app["form"],"notice":"Local mock portal only. No browser automation or external website access occurred."}

@router.post("/applications/{application_id}/mock-submit")
def mock_submit(application_id:str,payload:dict,user:dict=Depends(current_user)):
    if payload.get("confirm_demo_submission") is not True: raise HTTPException(422,"Explicit confirmation is required to create a simulated local receipt.")
    now=datetime.now(timezone.utc).isoformat(timespec="seconds");receipt="LOCAL-DEMO-"+secrets.token_hex(5).upper()
    with connection() as db:
        row=db.execute("SELECT status FROM applications WHERE id=? AND user_id=?",(application_id,user["id"])).fetchone()
        if not row: raise HTTPException(404,"Application draft not found.")
        db.execute("UPDATE applications SET status='demo_submitted',mock_receipt=?,updated_at=? WHERE id=?",(receipt,now,application_id))
        db.execute("INSERT INTO application_events(id,application_id,status,label,details,created_at) VALUES(?,?,?,?,?,?)",(str(uuid4()),application_id,"demo_submitted","Simulated local receipt created","This receipt exists only in this local CivicPilot demo.",now))
    record_audit(user["id"],"local_mock_submission","application",application_id)
    return {"status":"demo_submitted","receipt":receipt,"created_at":now,"demo_only":True,"notice":"This is a simulated local receipt. No government application was submitted."}

@router.get("/applications/{application_id}/export")
def export_app(application_id:str,user:dict=Depends(current_user)):
    from fastapi.responses import Response
    app=get_app(application_id,user)
    from app.services.seed_data import _pdf_escape
    lines=["CivicPilot AI - Local Application Draft","DEMONSTRATION ONLY - NOT SUBMITTED",app["scheme_name"],"Reference: "+app["reference_id"],"",json.dumps(app["form"],ensure_ascii=True,indent=2)[:5000]]
    commands=["BT","/F1 11 Tf","50 790 Td","14 TL"]
    for i,line in enumerate("\n".join(lines).splitlines()):
        if i: commands.append("T*")
        commands.append(f"({_pdf_escape(line[:160])}) Tj")
    commands.append("ET");stream="\n".join(commands).encode("ascii")
    objs=[b"<< /Type /Catalog /Pages 2 0 R >>",b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",b"<< /Length "+str(len(stream)).encode()+b" >>\nstream\n"+stream+b"\nendstream"]
    pdf=bytearray(b"%PDF-1.4\n");offsets=[0]
    for n,obj in enumerate(objs,1):offsets.append(len(pdf));pdf.extend(f"{n} 0 obj\n".encode()+obj+b"\nendobj\n")
    xref=len(pdf);pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for off in offsets[1:]:pdf.extend(f"{off:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return Response(content=bytes(pdf),media_type="application/pdf",headers={"Content-Disposition":f'attachment; filename="{app["reference_id"]}.pdf"'})
