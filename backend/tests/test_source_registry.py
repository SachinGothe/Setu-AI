from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import nsp_connector, source_refresh, source_registry


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(source_registry, "_database_path", lambda: tmp_path / "registry.db")
    sources = source_registry.list_sources()
    nsp = next(source for source in sources if source["display_name"] == "National Scholarship Portal")
    return nsp


def enable_nsp(source):
    return source_registry.update_source(source["id"], {
        "status": "verified",
        "enabled": True,
        "approved_urls": [nsp_connector.NSP_HOME_URL],
        "permission_notes": "Access and terms reviewed by the test fixture.",
        "rate_limit_seconds": 60,
    })


def test_registry_seeds_unverified_candidates(isolated_registry):
    sources = source_registry.list_sources()
    assert len(sources) == 4
    assert all(source["status"] == "pending_verification" for source in sources)
    assert all(source["enabled"] is False for source in sources)
    assert isolated_registry["approved_urls"] == []


def test_source_cannot_be_enabled_before_verification(isolated_registry):
    with pytest.raises(ValueError, match="verified"):
        source_registry.update_source(isolated_registry["id"], {"enabled": True})


def test_connector_extracts_public_text_and_hashes_content(monkeypatch):
    monkeypatch.setattr(nsp_connector, "_assert_public_resolution", lambda host: None)
    body = b"<html><title>NSP Portal</title><script>ignore this</script><h1>Announcements</h1><p>Public page text</p></html>"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=body))

    page = nsp_connector.fetch_nsp_homepage(nsp_connector.NSP_HOME_URL, [nsp_connector.NSP_HOME_URL], transport=transport)

    assert page.http_status == 200
    assert page.title == "NSP Portal"
    assert "Public page text" in page.content
    assert "ignore this" not in page.content
    assert len(page.content_sha256) == 64


def test_connector_rejects_url_outside_exact_allowlist(monkeypatch):
    monkeypatch.setattr(nsp_connector, "_assert_public_resolution", lambda host: None)
    with pytest.raises(nsp_connector.SourceFetchError, match="explicit allowlist"):
        nsp_connector.fetch_nsp_homepage("https://scholarships.gov.in/Public", ["https://scholarships.gov.in/Public"])


def test_connector_rejects_private_ip_resolution(monkeypatch):
    monkeypatch.setattr(nsp_connector.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(nsp_connector.SourceFetchError, match="non-public IP"):
        nsp_connector.fetch_nsp_homepage(nsp_connector.NSP_HOME_URL, [nsp_connector.NSP_HOME_URL])


def test_connector_rejects_redirect_outside_allowlist(monkeypatch):
    monkeypatch.setattr(nsp_connector, "_assert_public_resolution", lambda host: None)
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "https://example.com/"})
    transport = httpx.MockTransport(handler)

    with pytest.raises(nsp_connector.SourceFetchError, match="redirected outside"):
        nsp_connector.fetch_nsp_homepage(nsp_connector.NSP_HOME_URL, [nsp_connector.NSP_HOME_URL], transport=transport)


def test_connector_respects_robots_disallow_rule(monkeypatch):
    monkeypatch.setattr(nsp_connector, "_assert_public_resolution", lambda host: None)
    page_calls = []
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /", headers={"content-type": "text/plain"})
        page_calls.append(request.url.path)
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>not fetched</p>")
    transport = httpx.MockTransport(handler)

    with pytest.raises(nsp_connector.SourceFetchError, match="robots.txt disallows"):
        nsp_connector.fetch_nsp_homepage(nsp_connector.NSP_HOME_URL, [nsp_connector.NSP_HOME_URL], transport=transport)
    assert page_calls == []


def test_successful_refresh_persists_real_response_metadata_and_content(isolated_registry, monkeypatch):
    enable_nsp(isolated_registry)
    monkeypatch.setattr(nsp_connector, "_assert_public_resolution", lambda host: None)
    monkeypatch.setattr(source_refresh, "get_settings", lambda: SimpleNamespace(source_timeout_seconds=3, source_user_agent="CivicPilot-test/1.0"))
    body = b"<html><title>Official NSP</title><h1>Public announcement</h1><p>Retrieved content fixture.</p></html>"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=body))
    real_fetch = nsp_connector.fetch_nsp_homepage
    monkeypatch.setattr(source_refresh, "fetch_nsp_homepage", lambda url, allowlist, **kwargs: real_fetch(url, allowlist, transport=transport, **kwargs))

    run = source_refresh.refresh_source(isolated_registry["id"], network_allowed=True)
    updated = source_registry.get_source(isolated_registry["id"])

    assert run["status"] == "success"
    assert run["http_status"] == 200
    assert run["source_url"] == nsp_connector.NSP_HOME_URL
    assert updated["last_success_at"] is not None
    assert updated["last_http_status"] == 200
    assert updated["content_available"] is True
    assert source_registry.source_history(isolated_registry["id"])[0]["status"] == "success"


def test_failed_http_refresh_is_saved_without_false_success(isolated_registry, monkeypatch):
    enable_nsp(isolated_registry)
    monkeypatch.setattr(nsp_connector, "_assert_public_resolution", lambda host: None)
    monkeypatch.setattr(source_refresh, "get_settings", lambda: SimpleNamespace(source_timeout_seconds=3, source_user_agent="CivicPilot-test/1.0"))
    def failure_handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(503, headers={"content-type": "text/html"}, text="Unavailable")
    transport = httpx.MockTransport(failure_handler)
    real_fetch = nsp_connector.fetch_nsp_homepage
    monkeypatch.setattr(source_refresh, "fetch_nsp_homepage", lambda url, allowlist, **kwargs: real_fetch(url, allowlist, transport=transport, **kwargs))

    run = source_refresh.refresh_source(isolated_registry["id"], network_allowed=True)
    updated = source_registry.get_source(isolated_registry["id"])

    assert run["status"] == "failed"
    assert run["http_status"] == 503
    assert updated["last_success_at"] is None
    assert updated["last_error"] == "The source returned HTTP 503."
    assert source_refresh.health_status(updated) == "failed"


def test_unimplemented_source_connector_is_recorded_without_network_call(isolated_registry):
    candidate = next(source for source in source_registry.list_sources() if source["display_name"] == "PM Vishwakarma")
    source_registry.update_source(candidate["id"], {
        "status": "verified", "enabled": True, "approved_urls": [candidate["official_url"]],
        "permission_notes": "Test fixture only.",
    })

    with pytest.raises(LookupError, match="no network request was made"):
        source_refresh.refresh_source(candidate["id"], network_allowed=True)

    updated = source_registry.get_source(candidate["id"])
    run = source_registry.source_history(candidate["id"])[0]
    assert updated["last_checked_at"] is not None
    assert updated["last_success_at"] is None
    assert run["status"] == "connector_unavailable"
    assert run["http_status"] is None


def test_second_refresh_is_rate_limited(isolated_registry, monkeypatch):
    enable_nsp(isolated_registry)
    monkeypatch.setattr(nsp_connector, "_assert_public_resolution", lambda host: None)
    monkeypatch.setattr(source_refresh, "get_settings", lambda: SimpleNamespace(source_timeout_seconds=3, source_user_agent="CivicPilot-test/1.0"))
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<title>NSP</title><p>Content</p>")

    transport = httpx.MockTransport(handler)
    real_fetch = nsp_connector.fetch_nsp_homepage
    monkeypatch.setattr(source_refresh, "fetch_nsp_homepage", lambda url, allowlist, **kwargs: real_fetch(url, allowlist, transport=transport, **kwargs))
    source_refresh.refresh_source(isolated_registry["id"], network_allowed=True)

    second = source_refresh.refresh_source(isolated_registry["id"], network_allowed=True)

    assert calls == 2  # robots.txt and the homepage on the first attempt only
    assert second["status"] == "rate_limited"


def test_public_api_lists_candidates_without_claiming_a_connection(isolated_registry):
    response = TestClient(app).get("/api/v1/sources")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 4
    assert response.json()["connector_status"] == "nsp_homepage_only"
    assert all(item["health_status"] == "unverified" for item in response.json()["items"])


def test_source_admin_requires_authenticated_admin_role():
    response = TestClient(app).post("/api/v1/admin/sources/refresh-all")
    assert response.status_code == 401


def test_source_health_reports_connected_stale_disabled_and_unverified():
    now = datetime.now(timezone.utc)
    base = {"status": "verified", "enabled": True, "last_error": None, "last_success_at": now.isoformat()}
    assert source_refresh.health_status(base, now=now) == "connected"
    assert source_refresh.health_status({**base, "last_success_at": (now - timedelta(hours=49)).isoformat()}, now=now) == "stale"
    assert source_refresh.health_status({**base, "enabled": False}, now=now) == "disabled"
    assert source_refresh.health_status({**base, "status": "pending_verification"}, now=now) == "unverified"
