"""Framework integrations: Django, Flask, FastAPI/Starlette (ASGI), Celery, and
outgoing HTTP clients (requests, httpx). Each one records incoming requests per
route template and captures unhandled exceptions; see docs/10 §10.7."""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from .. import capture_exception, get_client
from .. import events as ev
from ..client import current_user, set_user


def request_id_from(headers_get) -> str:
    value = headers_get("x-request-id") or headers_get("X-Request-Id")
    return str(value)[:128] if value else uuid.uuid4().hex


class RequestRecorder:
    """One incoming request: start it, (optionally) note an exception, finish it."""

    __slots__ = ("blocked", "failed", "method", "request_id", "started", "trust_extra")

    def __init__(self, method: str, request_id: str):
        self.method = method
        self.request_id = request_id
        self.started = time.perf_counter()
        self.failed = False
        self.trust_extra: Optional[dict] = None
        self.blocked = False
        set_user(None)

    def exception(self, exc: BaseException, route: str, mechanism: str) -> None:
        self.failed = True
        capture_exception(exc, handled=False, mechanism=mechanism,
                          request={"method": self.method, "route": route, "request_id": self.request_id})

    def finish(self, route: str, status: int, request_bytes: Optional[int] = None,
               response_bytes: Optional[int] = None) -> None:
        client = get_client()
        if client is None or not client.enabled or not client.options.capture_requests:
            return
        try:
            if client.ignored_route(route):
                return
            extra = dict(self.trust_extra or {})
            if self.blocked:
                extra["blocked"] = True
            client.capture(ev.request_event(
                method=self.method, route=route, status=status,
                duration_ms=(time.perf_counter() - self.started) * 1000,
                request_bytes=request_bytes, response_bytes=response_bytes, request_id=self.request_id,
                failed=self.failed, user_id=current_user() if client.options.send_user_id else None,
                extra=extra or None,
            ))
        except Exception:  # noqa: BLE001
            pass


def trust_check(request: Any, recorder: RequestRecorder, *, headers_get, query_get, peer: Optional[str]) -> bool:
    """API Trust capture for this request; returns True when it must be blocked (opt-in)."""
    client = get_client()
    trust = getattr(client, "trust", None) if client is not None else None
    if trust is None or not trust.active:
        return False
    try:
        extra, blocked = trust.inspect(request, headers_get=headers_get, query_get=query_get, peer=peer)
    except Exception:  # noqa: BLE001 — fail open
        return False
    recorder.trust_extra = extra
    recorder.blocked = blocked
    return blocked


BLOCKED_BODY = b'{"error":"consumer_blocked"}'


def int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
