"""Low-volume retrieval of the public National Scholarship Portal homepage.

This connector is intentionally limited to one exact public URL. It does not
follow off-host redirects or enter account, application, or protected areas.
"""

import hashlib
import html
import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx


NSP_HOME_URL = "https://scholarships.gov.in/"
NSP_ROBOTS_URL = "https://scholarships.gov.in/robots.txt"
NSP_ALLOWED_URLS = frozenset({NSP_HOME_URL})
MAX_RESPONSE_BYTES = 2_000_000
MAX_ROBOTS_BYTES = 64_000
MAX_REDIRECTS = 2


class SourceFetchError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RetrievedPage:
    url: str
    http_status: int
    retrieved_at: str
    title: str
    content: str
    content_sha256: str


class _VisibleTextParser(HTMLParser):
    _skip_tags = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._skip_tags:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "h1", "h2", "h3", "li", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip_tags and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in {"p", "h1", "h2", "h3", "li", "br"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        cleaned = " ".join(html.unescape(data).split())
        if cleaned:
            self.parts.append(cleaned)
            if self._in_title:
                self.title_parts.append(cleaned)


def _assert_public_resolution(host: str) -> None:
    """Reject private, loopback, link-local, and reserved DNS results."""
    try:
        results = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceFetchError("Could not resolve the approved source hostname.") from exc
    if not results:
        raise SourceFetchError("The approved source hostname returned no addresses.")
    for result in results:
        address = ipaddress.ip_address(result[4][0])
        if not address.is_global:
            raise SourceFetchError("The approved source resolved to a non-public IP address.")


def _validate_target(url: str, approved_urls: set[str] | frozenset[str]) -> str:
    parsed = urlparse(url)
    if url not in NSP_ALLOWED_URLS or url not in approved_urls:
        raise SourceFetchError("The URL is outside the connector's explicit allowlist.")
    if parsed.scheme != "https" or parsed.hostname != "scholarships.gov.in" or parsed.path != "/" or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise SourceFetchError("The NSP connector only allows its exact public HTTPS homepage.")
    _assert_public_resolution(parsed.hostname)
    return url


def _robots_policy(client: httpx.Client, user_agent: str, configured_interval: int) -> None:
    """Read the exact host robots file and fail closed on policy errors."""
    parsed = urlparse(NSP_ROBOTS_URL)
    _assert_public_resolution(parsed.hostname or "")
    try:
        with client.stream("GET", NSP_ROBOTS_URL) as response:
            if response.is_redirect:
                raise SourceFetchError("robots.txt redirected; connector stopped for review.", response.status_code)
            if response.status_code == 404:
                return
            if response.status_code != 200:
                raise SourceFetchError(f"robots.txt returned HTTP {response.status_code}; connector stopped for review.", response.status_code)
            if int(response.headers.get("content-length", "0") or 0) > MAX_ROBOTS_BYTES:
                raise SourceFetchError("robots.txt exceeded the 64 KB limit.", response.status_code)
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_ROBOTS_BYTES:
                    raise SourceFetchError("robots.txt exceeded the 64 KB limit.", response.status_code)
    except httpx.TimeoutException as exc:
        raise SourceFetchError("robots.txt request timed out; connector stopped for review.") from exc
    except httpx.RequestError as exc:
        raise SourceFetchError(f"robots.txt request failed: {exc.__class__.__name__}; connector stopped for review.") from exc

    text = bytes(body).decode("utf-8", errors="replace")
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in {"allow", "disallow", "crawl-delay"} and agents:
            rules.append((key, value))
    if agents:
        groups.append((agents, rules))

    product = user_agent.split("/", 1)[0].lower()
    specific = [group for group in groups if any(agent != "*" and agent in product for agent in group[0])]
    matched = specific or [group for group in groups if "*" in group[0]]
    applicable = [rule for _, group_rules in matched for rule in group_rules]
    delays = [float(value) for key, value in applicable if key == "crawl-delay" and value.replace(".", "", 1).isdigit()]
    if delays and max(delays) > configured_interval:
        raise SourceFetchError("robots.txt requires a longer delay than this source's configured interval.")

    path = "/"
    matched_rules: list[tuple[int, str]] = []
    for key, value in applicable:
        if key not in {"allow", "disallow"} or not value:
            continue
        pattern = "^" + re.escape(value).replace(r"\*", ".*")
        if value.endswith("$"):
            pattern = pattern[:-2] + "$"
        else:
            pattern += ".*"
        if re.match(pattern, path):
            matched_rules.append((len(value), key))
    if matched_rules:
        longest = max(length for length, _ in matched_rules)
        if any(key == "disallow" for length, key in matched_rules if length == longest):
            raise SourceFetchError("robots.txt disallows the public homepage for this user agent.")


def fetch_nsp_homepage(
    url: str,
    approved_urls: list[str] | set[str] | frozenset[str],
    *,
    timeout_seconds: float = 12,
    user_agent: str = "CivicPilotAI-SourceMonitor/1.0",
    minimum_interval_seconds: int = 86400,
    transport: httpx.BaseTransport | None = None,
) -> RetrievedPage:
    target = _validate_target(url, set(approved_urls))
    headers = {"User-Agent": user_agent, "Accept": "text/html", "Accept-Encoding": "gzip, deflate"}
    client_args: dict = {"timeout": httpx.Timeout(timeout_seconds), "follow_redirects": False, "headers": headers}
    if transport is not None:
        client_args["transport"] = transport

    try:
        with httpx.Client(**client_args) as client:
            _robots_policy(client, user_agent, minimum_interval_seconds)
            response_status: int | None = None
            response_content: bytes | None = None
            response_encoding = "utf-8"
            for _ in range(MAX_REDIRECTS + 1):
                with client.stream("GET", target) as response:
                    response_status = response.status_code
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise SourceFetchError("The source returned a redirect without a target.", response.status_code)
                        next_target = urljoin(target, location)
                        if next_target not in approved_urls or next_target not in NSP_ALLOWED_URLS:
                            raise SourceFetchError("The source redirected outside the explicit allowlist.", response.status_code)
                        target = _validate_target(next_target, set(approved_urls))
                        continue
                    if response.status_code != 200:
                        raise SourceFetchError(f"The source returned HTTP {response.status_code}.", response.status_code)
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" not in content_type:
                        raise SourceFetchError("The approved homepage did not return HTML.", response.status_code)
                    if int(response.headers.get("content-length", "0") or 0) > MAX_RESPONSE_BYTES:
                        raise SourceFetchError("The source response exceeded the 2 MB limit.", response.status_code)
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise SourceFetchError("The source response exceeded the 2 MB limit.", response.status_code)
                    response_content = bytes(body)
                    response_encoding = response.encoding or "utf-8"
                    break
            else:
                raise SourceFetchError("The source exceeded the redirect limit.", response_status)
    except httpx.TimeoutException as exc:
        raise SourceFetchError("The source request timed out.") from exc
    except httpx.RequestError as exc:
        raise SourceFetchError(f"The source request failed: {exc.__class__.__name__}.") from exc

    if response_status != 200 or response_content is None:
        raise SourceFetchError("The source did not return a complete HTTP 200 response.", response_status)
    source_html = response_content.decode(response_encoding, errors="replace")
    parser = _VisibleTextParser()
    parser.feed(source_html)
    content = "\n".join(line.strip() for line in " ".join(parser.parts).splitlines() if line.strip())
    if not content:
        raise SourceFetchError("No readable page text was found.", response.status_code)
    title = " ".join(parser.title_parts).strip() or "National Scholarship Portal"
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return RetrievedPage(target, response_status, retrieved_at, title, content, hashlib.sha256(content.encode("utf-8")).hexdigest())
