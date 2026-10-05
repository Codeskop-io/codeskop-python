"""FastAPI / Starlette (any ASGI app): `app.add_middleware(CodeskopMiddleware)`.

Records every request by route template (`/orders/{order_id}`), captures
unhandled exceptions (and re-raises them), and reads the user ID from
`request.state.user_id` / `scope["user"]` when your auth sets it.
"""
from __future__ import annotations

from urllib.parse import parse_qs

from .. import events as ev
from ..client import set_framework, set_user
from . import BLOCKED_BODY, RequestRecorder, request_id_from, trust_check


class CodeskopMiddleware:
    def __init__(self, app):
        self.app = app
        try:
            from importlib.metadata import version

            try:
                set_framework(f"fastapi {version('fastapi')}")
            except Exception:  # noqa: BLE001
                set_framework(f"starlette {version('starlette')}")
        except Exception:  # noqa: BLE001
            set_framework("asgi")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
        recorder = RequestRecorder(scope.get("method", "GET"), request_id_from(headers.get))
        client = scope.get("client")
        peer = client[0] if client else None
        if trust_check(scope, recorder, headers_get=headers.get, query_get=lambda k: (query.get(k) or [None])[0], peer=peer):
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": BLOCKED_BODY})
            recorder.finish(ev.normalize_path(scope.get("path", "/")), 403)
            return
        state = {"status": 500, "bytes": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
            elif message["type"] == "http.response.body":
                state["bytes"] += len(message.get("body") or b"")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            _user(scope)
            recorder.exception(exc, _route(scope), "asgi")
            recorder.finish(_route(scope), 500, _int(headers.get("content-length")))
            raise
        _user(scope)
        recorder.finish(_route(scope), state["status"], _int(headers.get("content-length")), state["bytes"])


def _route(scope) -> str:
    route = scope.get("route") or _match_route(scope)
    path = getattr(route, "path", None) or getattr(route, "path_format", None)
    if path:
        return ev.template_route(path)
    return ev.normalize_path(scope.get("path", "/"))


def _match_route(scope):
    """Older Starlette doesn't put the matched route in the scope: find it in the app's routes."""
    app = scope.get("app")
    routes = getattr(getattr(app, "router", None), "routes", None) or getattr(app, "routes", None) or []
    try:
        from starlette.routing import Match
    except Exception:  # noqa: BLE001
        return None
    for route in routes:
        try:
            match, _ = route.matches({**scope, "type": "http"})
        except Exception:  # noqa: BLE001
            continue
        if match == Match.FULL:
            return route
    return None


def _user(scope) -> None:
    state = scope.get("state") or {}
    uid = state.get("user_id") if isinstance(state, dict) else None
    if uid is None:
        user = scope.get("user")
        try:
            if user is not None and getattr(user, "is_authenticated", False):
                uid = getattr(user, "identity", None) or getattr(user, "id", None)
        except Exception:  # noqa: BLE001
            uid = None
    if uid is not None:
        set_user(uid)


def _int(value):
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None
