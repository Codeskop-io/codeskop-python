"""Outgoing HTTP calls from `requests` and `httpx` → `api_timing` / `api_error`.

Installed by `codeskop.init()` when `capture_outgoing` is on and the library is
importable. Calls to the Codeskop endpoint itself are never recorded.
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

from .. import events as ev
from .. import get_client
from ..client import current_user

_installed = set()


def _record(method: str, url: str, status, started: float, error_kind=None, request_bytes=None, response_bytes=None):
    client = get_client()
    if client is None or not client.enabled or not client.options.capture_outgoing:
        return
    try:
        parts = urlsplit(str(url))
        host = parts.hostname or ""
        if not host or client.options.endpoint.split("//", 1)[-1].split("/", 1)[0].split(":")[0] == host:
            return
        for event in ev.outgoing_events(method=method, host=host, path=parts.path or "/", status=status,
                                        duration_ms=(time.perf_counter() - started) * 1000, error_kind=error_kind,
                                        request_bytes=request_bytes, response_bytes=response_bytes,
                                        user_id=current_user() if client.options.send_user_id else None):
            client.capture(event)
    except Exception:  # noqa: BLE001
        pass


def _error_kind(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    return "timeout" if "timeout" in name else "network_error"


def _length(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _install_requests() -> None:
    try:
        import requests
    except ImportError:
        return
    original = requests.Session.send
    if getattr(original, "_codeskop", False):
        return

    def send(self, request, **kwargs):
        started = time.perf_counter()
        body = request.body
        req_bytes = len(body) if isinstance(body, (bytes, str)) else None
        try:
            response = original(self, request, **kwargs)
        except Exception as exc:
            _record(request.method, request.url, None, started, _error_kind(exc), req_bytes)
            raise
        _record(request.method, request.url, response.status_code, started, None, req_bytes,
                _length(response.headers.get("content-length")))
        return response

    send._codeskop = True  # type: ignore[attr-defined]
    requests.Session.send = send
    _installed.add("requests")


def _install_httpx() -> None:
    try:
        import httpx
    except ImportError:
        return
    sync_original, async_original = httpx.Client.send, httpx.AsyncClient.send
    if getattr(sync_original, "_codeskop", False):
        return

    def send(self, request, **kwargs):
        started = time.perf_counter()
        try:
            response = sync_original(self, request, **kwargs)
        except Exception as exc:
            _record(request.method, request.url, None, started, _error_kind(exc))
            raise
        _record(request.method, request.url, response.status_code, started, None, None,
                _length(response.headers.get("content-length")))
        return response

    async def asend(self, request, **kwargs):
        started = time.perf_counter()
        try:
            response = await async_original(self, request, **kwargs)
        except Exception as exc:
            _record(request.method, request.url, None, started, _error_kind(exc))
            raise
        _record(request.method, request.url, response.status_code, started, None, None,
                _length(response.headers.get("content-length")))
        return response

    send._codeskop = True  # type: ignore[attr-defined]
    asend._codeskop = True  # type: ignore[attr-defined]
    httpx.Client.send = send
    httpx.AsyncClient.send = asend
    _installed.add("httpx")


def install() -> None:
    _install_requests()
    _install_httpx()
