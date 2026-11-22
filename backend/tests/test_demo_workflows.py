from io import BytesIO
from PIL import Image
import pytest
import fitz
from fastapi.testclient import TestClient

from app.core import database
from app.main import app
from app.services.admin_bootstrap import bootstrap_admin
from app.core.rate_limit import _events


@pytest.fixture
def client(tmp_path, monkeypatch):
    _events.clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "demo.db"))
    monkeypatch.setenv("SOURCE_DATABASE_PATH", str(tmp_path / "demo.db"))
    monkeypatch.setenv("APP_SECRET_KEY", "test-only-secret-key")
    from app.core import config
    config.get_settings.cache_clear()
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "demo.db")
    with TestClient(app) as test_client:
        yield test_client
    config.get_settings.cache_clear()


def register(client):
    response = client.post("/api/v1/auth/register", json={"email":"person@example.test","password":"strong-passphrase","display_name":"Demo Person"})
    assert response.status_code == 201
    client.headers.update({"Authorization": f"Bearer {response.json()['access_token']}"})


def create_admin_session(client, email="admin@example.test", password="admin-test-passphrase"):
    registered = client.post("/api/v1/auth/register", json={"email":email,"password":"initial-test-passphrase","display_name":"Admin Test"})
    assert registered.status_code == 201
    assert bootstrap_admin(email, password) == "promoted"
    login = client.post("/api/v1/auth/login", json={"email":email,"password":password})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, login.json()["user"]["id"]


def test_public_registration_always_creates_a_normal_user_and_profile_reports_role(client):
    response = client.post("/api/v1/auth/register", json={
        "email": "person@example.test", "password": "strong-passphrase", "display_name": "Ordinary User", "role": "admin",
    })
    assert response.status_code == 201
    assert response.json()["user"]["role"] == "user"
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=headers).json()["role"] == "user"
    profile = client.get("/api/v1/profile", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["role"] == "user"
    login = client.post("/api/v1/auth/login", json={"email": "PERSON@example.test", "password": "strong-passphrase"})
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "user"
    assert client.get("/api/v1/schemes", headers=headers).json()["items"] == []
    assert client.post("/api/v1/eligibility/check", headers=headers, json={"scheme_id": "demo-student-support", "answers": {}}).status_code == 404


def test_malformed_session_tokens_are_rejected(client):
    for value in ("not-a-token", "a.b.c", "eyJhbGciOiJub25lIn0.e30.signature"):
        response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {value}"})
        assert response.status_code == 401


def test_document_upload_is_private_and_persisted(client):
    register(client)
    image = Image.new("RGB", (4, 4), "white")
    output = BytesIO(); image.save(output, format="PNG")
    response = client.post("/api/v1/documents", files={"file":("sample.png",output.getvalue(),"image/png")},data={"title":"Private sample"})
    assert response.status_code == 201
    document = response.json()
    assert document["title"] == "Private sample"
    assert document["processing_status"] in {"ocr_used", "ocr_unavailable", "failed", "complete"}
    assert any(item["id"] == document["id"] for item in client.get("/api/v1/documents").json()["items"])


def test_no_public_source_refresh_request_is_performed(client):
    register(client)
    sources = client.get("/api/v1/sources").json()["items"]
    source = sources[0]
    response = client.post(f"/api/v1/admin/sources/{source['id']}/refresh",headers={"X-Admin-Token":"not-configured"})
    assert response.status_code == 403


def test_normal_user_cannot_access_admin_dashboard_or_knowledge_api(client):
    register(client)
    assert client.get("/api/v1/admin/overview").status_code == 403
    assert client.get("/api/v1/admin/knowledge-documents").status_code == 403
    assert client.patch("/api/v1/admin/users/another-user/role", json={"role":"admin"}).status_code == 403
    assert client.post("/api/v1/admin/sources", json={"display_name":"Bad Source","official_url":"https://example.gov"}).status_code == 403


def test_admin_source_controls_remain_read_only(client):
    headers, _ = create_admin_session(client)
    source = client.get("/api/v1/sources").json()["items"][0]
    before = (source["display_name"], source["status"], source["enabled"])
    assert client.post("/api/v1/admin/sources", headers=headers, json={"display_name":"New","official_url":"https://example.gov"}).status_code == 410
    assert client.patch(f"/api/v1/admin/sources/{source['id']}", headers=headers, json={"display_name":"Changed"}).status_code == 410
    assert client.post(f"/api/v1/admin/sources/{source['id']}/refresh", headers=headers).status_code == 410
    after = client.get("/api/v1/sources").json()["items"][0]
    assert (after["display_name"], after["status"], after["enabled"]) == before


def test_bootstrap_promotes_existing_user_and_preserves_profile(client):
    registered = client.post("/api/v1/auth/register", json={
        "email":"promote@example.test", "password":"initial-passphrase", "display_name":"Existing User",
    })
    assert registered.status_code == 201
    user_id = registered.json()["user"]["id"]
    client.headers.update({"Authorization": f"Bearer {registered.json()['access_token']}"})
    assert client.put("/api/v1/profile", json={"state":"Karnataka"}).status_code == 200
    with database.connection() as db:
        scheme_id = "test-archived-scheme"
        db.execute("INSERT INTO schemes(id,name,category,department,summary,description,target_beneficiaries,benefits,eligibility_rules,required_documents,application_steps,important_notes,source_document_ids,demo_only,is_active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?)", (
            scheme_id, "Verified scheme fixture", "Test", "Test department", "Test summary", "Test description", "[]", "[]", "{}", "[]", "[]", "[]", "[]", "2026-01-01",
        ))
        db.execute("INSERT INTO documents(id,owner_user_id,title,original_filename,mime_type,size_bytes,document_kind,source_name,uploaded_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "preserved-doc", user_id, "Existing document", "existing.png", "image/png", 8, "supporting_document", "User upload", "2026-01-01", "2026-01-01",
        ))
        db.execute("INSERT INTO applications(id,user_id,scheme_id,reference_id,status,form_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (
            "preserved-app", user_id, scheme_id, "DEMO-PRESERVE", "draft", "{}", "2026-01-01", "2026-01-01",
        ))

    assert bootstrap_admin("promote@example.test", "new-admin-passphrase") == "promoted"
    # Promotion revokes the old session, then normal login returns the database role.
    assert client.get("/api/v1/auth/me").status_code == 401
    login = client.post("/api/v1/auth/login", json={
        "email":"promote@example.test", "password":"new-admin-passphrase",
    })
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "admin"
    client.headers.update({"Authorization": f"Bearer {login.json()['access_token']}"})
    assert client.get("/api/v1/admin/overview").status_code == 200
    with database.connection() as db:
        account = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        profile = db.execute("SELECT state FROM user_profiles WHERE user_id=?", (user_id,)).fetchone()
    assert account["role"] == "admin"
    assert profile["state"] == "Karnataka"
    with database.connection() as db:
        assert db.execute("SELECT 1 FROM documents WHERE id='preserved-doc'").fetchone()
        assert db.execute("SELECT 1 FROM applications WHERE id='preserved-app'").fetchone()


def test_bootstrap_creates_a_missing_admin_account(client):
    assert bootstrap_admin("new-admin@example.test", "another-admin-passphrase") == "created"
    response = client.post("/api/v1/auth/login", json={
        "email":"new-admin@example.test", "password":"another-admin-passphrase",
    })
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "admin"
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    client.headers.update(headers)
    assert client.get("/api/v1/profile", headers=headers).json()["role"] == "admin"
    assert client.get("/api/v1/admin/overview").status_code == 200


def test_admin_document_upload_metadata_preview_citation_and_delete(client):
    headers, _ = create_admin_session(client)
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "SECTION 3 GENERAL DUTIES\nA fictional legal passage about tenant safety and notice.")
    data = pdf.tobytes()
    response = client.post("/api/v1/admin/knowledge-documents", headers=headers,
        files={"file":("tenant-act.pdf",data,"application/pdf")},
        data={"title":"Demo Tenant Act","source_name":"Demo Authority","issuing_authority":"Demo Authority","document_type":"Act","publication_date":"2024-05-10","source_url":"https://example.gov/tenant-act.pdf"})
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["issuing_authority"] == "Demo Authority"
    assert document["document_type"] == "Act"
    assert document["publication_date"] == "2024-05-10"
    assert document["processing_status"] == "text_extracted"

    preview = client.get(f"/api/v1/admin/knowledge-documents/{document['id']}", headers=headers)
    assert preview.status_code == 200
    assert preview.json()["pages"][0]["page_number"] == 1
    assert preview.json()["pages"][0]["section"] == "SECTION 3 GENERAL DUTIES"
    chunk = preview.json()["pages"][0]
    location = client.patch(f"/api/v1/admin/knowledge-documents/{document['id']}/passages/{chunk['id']}", headers=headers,
        json={"page_number": 1, "section": "SECTION 3"})
    assert location.status_code == 200
    assert location.json()["text"] == chunk["text"]
    edited = client.patch(f"/api/v1/admin/knowledge-documents/{document['id']}", headers=headers, json={
        "title": "Tenant Safety Reference", "issuing_authority": "Test Legal Authority", "document_type": "Public reference", "publication_date": "2024-05-10", "source_url": "https://example.gov/reference.pdf",
    })
    assert edited.status_code == 200
    assert edited.json()["title"] == "Tenant Safety Reference"

    ask = client.post("/api/v1/assistant/messages", headers=headers, json={"message":"tenant safety notice", "language":"en"})
    assert ask.status_code == 200
    citation = next(c for c in ask.json()["message"]["citations"] if c["document_id"] == document["id"])
    assert citation["title"] == "Tenant Safety Reference"
    assert citation["section"] == "SECTION 3"
    assert "tenant safety" in citation["text"]
    overview = client.get("/api/v1/admin/overview", headers=headers).json()
    assert overview["counts"]["total_uploaded_legal_documents"] == 1
    assert overview["counts"]["total_knowledge_documents"] == 1
    assert any(activity["action"] == "knowledge_document_uploaded" for activity in overview["recent_activities"])

    assert client.delete(f"/api/v1/admin/knowledge-documents/{document['id']}", headers=headers).status_code == 204
    assert client.get(f"/api/v1/admin/knowledge-documents/{document['id']}", headers=headers).status_code == 404


def test_demo_schemes_are_not_seeded_or_manageable_and_admin_self_lockout_is_blocked(client):
    headers, admin_id = create_admin_session(client)
    assert client.get("/api/v1/schemes", headers=headers).json()["items"] == []
    assert client.get("/api/v1/admin/overview", headers=headers).json()["counts"]["total_sample_schemes"] == 0
    assert client.patch(f"/api/v1/admin/users/{admin_id}/role",headers=headers,json={"role":"user"}).status_code == 409
    assert client.patch(f"/api/v1/admin/users/{admin_id}/active",headers=headers,json={"active":False}).status_code == 409
    assert client.get("/api/v1/admin/schemes", headers=headers).status_code == 404


def test_legacy_demo_cleanup_removes_retrieval_data_but_preserves_user_records(client):
    register(client)
    user = client.get("/api/v1/auth/me").json()
    with database.connection() as db:
        now = "2026-01-01"
        db.execute("INSERT INTO schemes(id,name,category,department,summary,description,target_beneficiaries,benefits,eligibility_rules,required_documents,application_steps,important_notes,source_document_ids,demo_only,is_active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,?)", (
            "legacy-demo", "Demo Learning Access Voucher", "Education", "Fiction", "Demo summary", "Demo content", "[]", "[]", "{}", "[]", "[]", "[]", "[]", now,
        ))
        db.execute("INSERT INTO applications(id,user_id,scheme_id,reference_id,status,form_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (
            "legacy-app", user["id"], "legacy-demo", "OLD-APP", "draft", "{}", now, now,
        ))
        db.execute("INSERT INTO documents(id,owner_user_id,title,original_filename,mime_type,size_bytes,document_kind,source_name,uploaded_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "legacy-private-doc", user["id"], "Private upload", "private.pdf", "application/pdf", 12, "supporting_document", "User upload", now, now,
        ))
        db.execute("INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,created_at) VALUES(?,?,?,?,?,?)", (
            "legacy-audit", user["id"], "existing_action", "application", "legacy-app", now,
        ))
        db.execute("INSERT INTO documents(id,title,original_filename,mime_type,size_bytes,document_kind,source_name,is_sample,content_text,uploaded_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            "legacy-sample-doc", "Demo scheme guide", "demo.html", "text/html", 20, "text/html", "Fiction", 1, "fictional passage", now, now,
        ))
        db.execute("INSERT INTO document_chunks(id,document_id,chunk_index,text,content_hash,created_at) VALUES(?,?,?,?,?,?)", ("legacy-sample-chunk", "legacy-sample-doc", 0, "fictional passage", "hash", now))
        db.execute("DELETE FROM app_migrations WHERE version='2026-11-remove-fictional-demo-content'")
    database._remove_legacy_demo_content()
    with database.connection() as db:
        assert db.execute("SELECT 1 FROM applications WHERE id='legacy-app'").fetchone()
        assert db.execute("SELECT 1 FROM documents WHERE id='legacy-private-doc'").fetchone()
        assert db.execute("SELECT 1 FROM audit_logs WHERE id='legacy-audit'").fetchone()
        archived = db.execute("SELECT name,is_active,description FROM schemes WHERE id='legacy-demo'").fetchone()
        assert archived["name"] == "Archived demonstration scheme" and archived["is_active"] == 0
        assert "Demo Learning Access Voucher" not in archived["description"]
        assert db.execute("SELECT 1 FROM documents WHERE id='legacy-sample-doc'").fetchone() is None
        assert db.execute("SELECT 1 FROM document_chunks WHERE id='legacy-sample-chunk'").fetchone() is None


def test_all_admin_knowledge_apis_deny_normal_users(client):
    admin_headers, _ = create_admin_session(client)
    user_response = client.post("/api/v1/auth/register", json={"email":"reader@example.test","password":"reader-passphrase","display_name":"Reader"})
    user_headers = {"Authorization": f"Bearer {user_response.json()['access_token']}"}
    pdf = fitz.open(); page = pdf.new_page(); page.insert_text((72, 72), "SECTION 2 SOURCE TEXT\nA real passage stays exactly as extracted.")
    file_data = pdf.tobytes()
    uploaded = client.post("/api/v1/admin/knowledge-documents", headers=admin_headers, files={"file":("reference.pdf",file_data,"application/pdf")}, data={"title":"Reference","source_name":"Authority","issuing_authority":"Authority","document_type":"Guideline"})
    assert uploaded.status_code == 201
    doc_id = uploaded.json()["id"]
    chunk_id = client.get(f"/api/v1/admin/knowledge-documents/{doc_id}",headers=admin_headers).json()["pages"][0]["id"]
    requests = [
        ("GET", "/api/v1/admin/knowledge-documents", None, None),
        ("GET", f"/api/v1/admin/knowledge-documents/{doc_id}", None, None),
        ("GET", f"/api/v1/admin/knowledge-documents/{doc_id}/download", None, None),
        ("POST", "/api/v1/admin/knowledge-documents", {"files":{"file":("x.pdf",file_data,"application/pdf")},"data":{"source_name":"Authority"}}, None),
        ("PATCH", f"/api/v1/admin/knowledge-documents/{doc_id}", {"json":{"title":"Changed","issuing_authority":"Authority","document_type":"Guideline"}}, None),
        ("PATCH", f"/api/v1/admin/knowledge-documents/{doc_id}/passages/{chunk_id}", {"json":{"page_number":1,"section":"SECTION 2"}}, None),
        ("POST", f"/api/v1/admin/knowledge-documents/{doc_id}/reprocess", None, None),
        ("GET", "/api/v1/admin/processing-jobs", None, None),
        ("DELETE", f"/api/v1/admin/knowledge-documents/{doc_id}", None, None),
    ]
    for method, url, kwargs, _ in requests:
        response = client.request(method, url, headers=user_headers, **(kwargs or {}))
        assert response.status_code == 403, (method, url, response.status_code)
    assert client.get("/api/v1/admin/knowledge-documents", headers=admin_headers).status_code == 200
    assert client.get(f"/api/v1/admin/knowledge-documents/{doc_id}/download", headers=admin_headers).status_code == 200
    assert client.post(f"/api/v1/admin/knowledge-documents/{doc_id}/reprocess", headers=admin_headers).status_code == 200
    assert client.delete(f"/api/v1/admin/knowledge-documents/{doc_id}", headers=admin_headers).status_code == 204


def test_admin_can_change_other_account_role_and_activation(client):
    admin_headers, _ = create_admin_session(client)
    registered = client.post("/api/v1/auth/register",json={"email":"managed@example.test","password":"managed-user-passphrase","display_name":"Managed User"})
    user_id = registered.json()["user"]["id"]
    user_headers = {"Authorization":f"Bearer {registered.json()['access_token']}"}

    changed = client.patch(f"/api/v1/admin/users/{user_id}/role",headers=admin_headers,json={"role":"admin"})
    assert changed.status_code == 200
    assert client.get("/api/v1/auth/me",headers=user_headers).status_code == 401
    promoted = client.post("/api/v1/auth/login",json={"email":"managed@example.test","password":"managed-user-passphrase"})
    assert promoted.json()["user"]["role"] == "admin"

    deactivated = client.patch(f"/api/v1/admin/users/{user_id}/active",headers=admin_headers,json={"active":False})
    assert deactivated.status_code == 200
    promoted_headers = {"Authorization":f"Bearer {promoted.json()['access_token']}"}
    assert client.get("/api/v1/admin/overview",headers=promoted_headers).status_code == 401
    assert client.post("/api/v1/auth/login",json={"email":"managed@example.test","password":"managed-user-passphrase"}).status_code == 401
    logs = client.get("/api/v1/admin/audit-logs",headers=admin_headers).json()["items"]
    assert {row["action"] for row in logs} >= {"user_role_changed","user_activation_changed"}


def test_admin_endpoints_never_return_password_hash_or_private_document_text(client):
    admin_headers, _ = create_admin_session(client)
    user_response=client.post("/api/v1/auth/register",json={"email":"private@example.test","password":"private-user-passphrase","display_name":"Private User"})
    user_id=user_response.json()["user"]["id"]
    with database.connection() as db:
        db.execute("INSERT INTO documents(id,owner_user_id,title,original_filename,mime_type,size_bytes,document_kind,source_name,content_text,uploaded_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(
            "private-admin-list-test",user_id,"Private identity file","identity.pdf","application/pdf",10,"supporting_document","User upload","private document secret text","2026-01-01","2026-01-01",
        ))
    users=client.get("/api/v1/admin/users",headers=admin_headers).json()["items"]
    listed=next(u for u in users if u["id"]==user_id)
    assert "password_hash" not in listed
    details=client.get(f"/api/v1/admin/users/{user_id}",headers=admin_headers).json()
    assert details["counts"]["documents"]==1
    assert "private document secret text" not in str(details)
