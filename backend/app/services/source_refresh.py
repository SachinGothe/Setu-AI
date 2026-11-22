from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.services import source_registry
from app.services.nsp_connector import NSP_HOME_URL, SourceFetchError, fetch_nsp_homepage


def refresh_source(source_id: str, *, network_allowed: bool = False) -> dict:
    if not network_allowed:
        raise PermissionError("Network source refresh is disabled by the local demo policy.")
    source = source_registry.get_source(source_id)
    if source is None:
        raise KeyError("Source not found.")
    if source["status"] != "verified" or not source["enabled"]:
        raise PermissionError("Only verified, enabled sources can be refreshed.")
    if source["official_url"] != NSP_HOME_URL or NSP_HOME_URL not in source["approved_urls"]:
        error = "No connector is configured for this source and allowlist; no network request was made."
        source_registry.record_unavailable_connector(source_id, error)
        raise LookupError(error)

    run = source_registry.begin_refresh(source_id)
    if run["status"] == "rate_limited":
        return run
    try:
        page = fetch_nsp_homepage(
            NSP_HOME_URL,
            source["approved_urls"],
            timeout_seconds=get_settings().source_timeout_seconds,
            user_agent=get_settings().source_user_agent,
            minimum_interval_seconds=source["rate_limit_seconds"],
        )
    except SourceFetchError as exc:
        return source_registry.finish_refresh_failure(run["id"], source_id, str(exc), exc.status_code)
    except Exception as exc:  # Record controlled operational failures; do not expose stack details.
        return source_registry.finish_refresh_failure(run["id"], source_id, f"Unexpected connector failure: {exc.__class__.__name__}.")
    return source_registry.finish_refresh_success(run["id"], source_id, page)


def health_status(source: dict, *, now: datetime | None = None) -> str:
    if source["status"] == "disabled":
        return "disabled"
    if source["status"] != "verified":
        return "unverified"
    if not source["enabled"]:
        return "disabled"
    if source.get("last_error"):
        return "failed"
    if not source.get("last_success_at"):
        return "unverified"
    current = now or datetime.now(timezone.utc)
    retrieved = datetime.fromisoformat(source["last_success_at"])
    if current - retrieved > timedelta(hours=48):
        return "stale"
    return "connected"
