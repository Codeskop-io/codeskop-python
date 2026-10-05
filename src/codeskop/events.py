"""Event builders (docs/10 §10.6): exceptions, inbound requests, outgoing calls."""
from __future__ import annotations

import os
import re
import sys
import sysconfig
import uuid
from datetime import datetime, timezone
from types import TracebackType
from typing import Optional

MAX_MESSAGE = 2048
MAX_FRAMES = 100

_NUMERIC = re.compile(r"^\d+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX = re.compile(r"^[0-9a-fA-F]{16,}$")
# <int:id>, <id>, <path:rest> (Django / Flask) → {id}
_ANGLE_PARAM = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def normalize_path(path: str) -> str:
    """Same templating as the server: drop the query, numeric/UUID/long-hex segments → {id}."""
    path = (path or "/").split("?", 1)[0].split("#", 1)[0]
    segs = ["{id}" if s and (_NUMERIC.match(s) or _UUID.match(s) or _HEX.match(s)) else s for s in path.split("/")]
    out = "/".join(segs)
    return out if out.startswith("/") else "/" + out


def template_route(route: str) -> str:
    """A framework route template in our `{name}` form: `users/<int:pk>/` → `/users/{pk}/`."""
    route = _ANGLE_PARAM.sub(lambda m: "{" + m.group(1) + "}", route or "/")
    route = route.lstrip("^").rstrip("$")
    return route if route.startswith("/") else "/" + route


# ---------------------------------------------------------------------------
# Stack traces
# ---------------------------------------------------------------------------

def _library_roots() -> tuple:
    roots = set()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            p = sysconfig.get_paths().get(key)
        except Exception:  # noqa: BLE001
            p = None
        if p:
            roots.add(os.path.realpath(p))
    for p in sys.path:
        if p and ("site-packages" in p or "dist-packages" in p):
            roots.add(os.path.realpath(p))
    return tuple(sorted(roots, key=len, reverse=True))


_LIB_ROOTS = _library_roots()
_SDK_DIR = os.path.dirname(os.path.realpath(__file__))


def is_in_app(filename: str, module: str) -> bool:
    if not filename or filename.startswith("<"):
        return False
    if module.split(".")[0] in ("codeskop",):
        return False
    real = os.path.realpath(filename)
    if real.startswith(_SDK_DIR):
        return False
    if "site-packages" in real or "dist-packages" in real:
        return False
    return not any(real.startswith(root + os.sep) for root in _LIB_ROOTS)


def frames_from_traceback(tb: Optional[TracebackType]) -> list:
    """Frames innermost first, as the backend expects."""
    frames = []
    while tb is not None:
        f = tb.tb_frame
        module = f.f_globals.get("__name__", "") or ""
        filename = f.f_code.co_filename
        frames.append({
            "class": module,
            "method": f.f_code.co_name,
            "file": _short_path(filename),
            "line": tb.tb_lineno,
            "in_app": is_in_app(filename, module),
        })
        tb = tb.tb_next
    frames.reverse()
    return frames[:MAX_FRAMES]


def _short_path(filename: str) -> str:
    try:
        rel = os.path.relpath(filename)
        return filename if rel.startswith("..") else rel
    except ValueError:
        return filename


def exception_class_name(exc: BaseException) -> str:
    cls = type(exc)
    module = cls.__module__
    return cls.__qualname__ if module in ("builtins", "__main__") else f"{module}.{cls.__qualname__}"


def _exception_payload(exc: BaseException, depth: int = 0) -> dict:
    payload = {
        "exception_class": exception_class_name(exc),
        "message": str(exc)[:MAX_MESSAGE],
        "stacktrace": frames_from_traceback(exc.__traceback__),
    }
    cause = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    if cause is not None and depth < 3:
        payload["cause"] = _exception_payload(cause, depth + 1)
    return payload


def exception_event(exc: BaseException, *, handled: bool, mechanism: str = "generic",
                    severity: Optional[str] = None, request: Optional[dict] = None,
                    user_id: Optional[str] = None, tags: Optional[dict] = None) -> dict:
    payload = _exception_payload(exc)
    payload["handled"] = handled
    payload["mechanism"] = mechanism
    if request:
        payload["request"] = request
    if tags:
        payload["tags"] = {str(k)[:64]: str(v)[:256] for k, v in list(tags.items())[:20]}
    return _event("exception", severity or ("medium" if handled else "high"), payload, user_id)


def message_event(message: str, severity: str = "medium", user_id: Optional[str] = None) -> dict:
    payload = {"exception_class": "Message", "message": str(message)[:MAX_MESSAGE], "stacktrace": [],
               "handled": True, "mechanism": "message"}
    return _event("exception", severity, payload, user_id)


def request_event(*, method: str, route: str, status: int, duration_ms: float, request_bytes: Optional[int] = None,
                  response_bytes: Optional[int] = None, request_id: Optional[str] = None, failed: bool = False,
                  user_id: Optional[str] = None, extra: Optional[dict] = None) -> dict:
    payload = {"method": (method or "GET").upper(), "route": route or "/", "status": int(status or 0),
               "duration_ms": round(float(duration_ms), 2)}
    if request_bytes is not None:
        payload["request_bytes"] = int(request_bytes)
    if response_bytes is not None:
        payload["response_bytes"] = int(response_bytes)
    if request_id:
        payload["request_id"] = request_id
    if extra:
        payload.update(extra)
    severity = "high" if failed or payload["status"] >= 500 else "low"
    return _event("http_request", severity, payload, user_id)


def outgoing_events(*, method: str, host: str, path: str, status: Optional[int], duration_ms: float,
                    error_kind: Optional[str] = None, request_bytes: Optional[int] = None,
                    response_bytes: Optional[int] = None, user_id: Optional[str] = None) -> list:
    """`api_timing` for every call plus `api_error` for failures (web/mobile parity)."""
    payload = {"method": (method or "GET").upper(), "host": host, "path": normalize_path(path),
               "duration_ms": round(float(duration_ms), 2)}
    if status is not None:
        payload["status"] = int(status)
    if request_bytes is not None:
        payload["request_bytes"] = int(request_bytes)
    if response_bytes is not None:
        payload["response_bytes"] = int(response_bytes)
    if error_kind is None and status is not None and status >= 400:
        error_kind = "http_5xx" if status >= 500 else "http_4xx"
    events = [_event("api_timing", "low", dict(payload), user_id)]
    if error_kind:
        events.append(_event("api_error", "high", {**payload, "error_kind": error_kind}, user_id))
    return events


def _event(type_: str, severity: str, payload: dict, user_id: Optional[str]) -> dict:
    event = {"event_id": new_id(), "type": type_, "severity": severity, "occurred_at": now_iso(), "payload": payload}
    if user_id:
        event["user"] = {"id": str(user_id)[:128]}
    return event
