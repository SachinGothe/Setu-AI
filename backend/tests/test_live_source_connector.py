"""Opt-in smoke test; run only after checking the source's current access terms."""

import pytest

from app.services.nsp_connector import NSP_HOME_URL, fetch_nsp_homepage


pytestmark = [
    pytest.mark.live,
    pytest.mark.skip(reason="Live government retrieval is excluded from the local demo by project policy."),
]


def test_live_nsp_homepage_returns_public_html():
    page = fetch_nsp_homepage(NSP_HOME_URL, [NSP_HOME_URL])

    assert page.http_status == 200
    assert "National Scholarship Portal" in page.content or "Scholarship" in page.title
    assert page.content_sha256
