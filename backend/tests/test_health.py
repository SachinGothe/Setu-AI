from fastapi.testclient import TestClient
from app.main import app


client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_public_config_discloses_independence():
    response = client.get("/api/v1/config/public")
    assert response.status_code == 200
    data = response.json()
    assert "not a government" in data["disclaimer"]
    assert data["languages"] == ["en", "kn", "hi"]


def test_loopback_frontend_alias_is_allowed_in_development():
    response = client.options("/api/v1/auth/login", headers={
        "Origin": "http://127.0.0.1:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_untrusted_frontend_origin_is_not_allowed():
    response = client.options("/api/v1/auth/login", headers={
        "Origin": "https://attacker.example",
        "Access-Control-Request-Method": "POST",
    })
    assert "access-control-allow-origin" not in response.headers
