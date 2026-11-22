"""Source-grounded local assistant; Gemini is optional and never fabricates citations."""
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.database import connection
from app.core.rate_limit import enforce_rate_limit
from app.core.security import current_user

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

class MessagePayload(BaseModel):
    message: str = Field(min_length=2, max_length=3000)
    conversation_id: str | None = None
    language: str = Field(default="en", pattern="^(en|kn|hi)$")

def _retrieve(query: str, limit: int = 5) -> list[dict]:
    terms = list(dict.fromkeys(re.findall(r"[\w-]{3,}", query.lower())))[:12]
    with connection() as db:
        rows = db.execute("SELECT c.text,c.page_number,c.section,d.id AS document_id,d.title,d.source_name,d.source_url,d.issuing_authority,d.document_type,d.publication_date FROM document_chunks c JOIN documents d ON d.id=c.document_id WHERE d.processing_status IN ('complete','text_extracted','ocr_used') AND d.document_kind='knowledge' AND d.is_sample=0 ORDER BY d.uploaded_at DESC,c.chunk_index LIMIT 500").fetchall()
    ranked=[]
    for row in rows:
        text=row["text"]
        score=sum(text.lower().count(term) for term in terms)
        if score:
            ranked.append((score,dict(row)))
    ranked.sort(key=lambda item:item[0],reverse=True)
    return [row for _,row in ranked[:limit]]

@router.get("/conversations")
def conversations(user: dict = Depends(current_user)) -> dict:
    with connection() as db:
        rows=db.execute("SELECT id,title,language,created_at,updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC",(user["id"],)).fetchall()
    return {"items":[dict(r) for r in rows]}

@router.post("/messages")
def ask(payload: MessagePayload, request: Request, user: dict = Depends(current_user)) -> dict:
    enforce_rate_limit(request,"assistant",limit=30,window_seconds=3600)
    now=datetime.now(timezone.utc).isoformat(timespec="seconds")
    conversation_id=payload.conversation_id
    with connection() as db:
        if conversation_id:
            conv=db.execute("SELECT id FROM conversations WHERE id=? AND user_id=?",(conversation_id,user["id"])).fetchone()
            if not conv: raise HTTPException(status_code=404,detail="Conversation not found.")
        else:
            conversation_id=str(uuid4())
            db.execute("INSERT INTO conversations(id,user_id,title,language,created_at,updated_at) VALUES(?,?,?,?,?,?)",(conversation_id,user["id"],payload.message[:80],payload.language,now,now))
        user_message_id=str(uuid4())
        db.execute("INSERT INTO chat_messages(id,conversation_id,role,content,created_at) VALUES(?,?,'user',?,?)",(user_message_id,conversation_id,payload.message,now))
    citations=_retrieve(payload.message)
    for number, citation in enumerate(citations, start=1):
        citation["citation_number"] = number
    settings=get_settings()
    answer="I found no matching passage in the administrator-managed knowledge repository. Try a question about an indexed document."
    if citations:
        excerpts="\n\n".join(f"[{i+1}] {c['title']}: {c['text']}" for i,c in enumerate(citations))
        if settings.gemini_api_key:
            try:
                from google import genai
                client=genai.Client(api_key=settings.gemini_api_key)
                response=client.models.generate_content(model=settings.gemini_model,contents=f"Answer in language code {payload.language}. Use only the provided passages. If they do not answer the question, say so. Cite source passages as [1], [2]. Do not infer eligibility or benefits. Only cite passage numbers included below.\nQuestion: {payload.message}\nPassages:\n{excerpts}")
                generated=(response.text or "").strip()
                references=[int(number) for number in re.findall(r"\[(\d+)\]", generated)]
                if generated and all(1 <= number <= len(citations) for number in references):
                    answer=generated
                    if not references:
                        answer += "\n\nSources: " + ", ".join(f"[{i}]" for i in range(1,len(citations)+1))
                else:
                    answer="Gemini returned an answer without valid references to retrieved passages. Retrieved source excerpts are listed below; no unsupported answer was shown."
            except Exception:
                answer="Gemini is configured but unavailable right now. Retrieved local excerpts are shown below; no generated answer was produced."
        else:
            answer="Gemini is not configured. Here are matching passages from the administrator-curated document library."
    assistant_id=str(uuid4())
    with connection() as db:
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?",(now,conversation_id))
        db.execute("INSERT INTO chat_messages(id,conversation_id,role,content,citations_json,created_at) VALUES(?,?,'assistant',?,?,?)",(assistant_id,conversation_id,answer,json.dumps(citations),now))
        history=[dict(r) for r in db.execute("SELECT id,role,content,citations_json,feedback,created_at FROM chat_messages WHERE conversation_id=? ORDER BY created_at",(conversation_id,)).fetchall()]
    return {"conversation_id":conversation_id,"message":{"id":assistant_id,"role":"assistant","content":answer,"citations":citations,"created_at":now},"history":history,"gemini_configured":bool(settings.gemini_api_key),"demo_only":False}

@router.get("/conversations/{conversation_id}")
def conversation(conversation_id: str,user:dict=Depends(current_user))->dict:
    with connection() as db:
        conv=db.execute("SELECT * FROM conversations WHERE id=? AND user_id=?",(conversation_id,user["id"])).fetchone()
        rows=db.execute("SELECT * FROM chat_messages WHERE conversation_id=? ORDER BY created_at",(conversation_id,)).fetchall()
    if not conv: raise HTTPException(status_code=404,detail="Conversation not found.")
    items=[]
    for row in rows:
        item=dict(row);item["citations"]=json.loads(item.pop("citations_json"));items.append(item)
    return {"conversation":dict(conv),"messages":items}

@router.delete("/conversations/{conversation_id}",status_code=204)
def delete_conversation(conversation_id:str,user:dict=Depends(current_user)):
    with connection() as db:
        db.execute("DELETE FROM conversations WHERE id=? AND user_id=?",(conversation_id,user["id"]))
