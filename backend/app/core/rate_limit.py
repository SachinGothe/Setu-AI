"""Small in-process rate limiter for local demo endpoints."""

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


_events: dict[str, deque[float]] = defaultdict(deque)


def enforce_rate_limit(request: Request, action: str, *, limit: int, window_seconds: int = 60) -> None:
    client = request.client.host if request.client else "unknown"
    key = f"{action}:{client}"
    now = time.monotonic()
    events = _events[key]
    while events and events[0] <= now - window_seconds:
        events.popleft()
    if len(events) >= limit:
        raise HTTPException(status_code=429, detail="Too many requests. Please wait and try again.", headers={"Retry-After": str(window_seconds)})
    events.append(now)
    if len(_events) > 10000:
        for stale_key in list(_events)[:1000]:
            if not _events[stale_key]:
                _events.pop(stale_key, None)
