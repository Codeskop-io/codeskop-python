"""Django: add `"codeskop.integrations.django.CodeskopMiddleware"` near the top of MIDDLEWARE.

Call `codeskop.init(...)` in settings.py (or wsgi.py / asgi.py). The middleware
records every request by URL pattern (`/orders/{pk}/`), captures unhandled view
exceptions, and attaches the signed-in user's primary key as the user ID.
"""
from __future__ import annotations

from .. import events as ev
from ..client import set_framework, set_user
from . import BLOCKED_BODY, RequestRecorder, int_or_none, request_id_from, trust_check


def _route(request) -> str:
    match = getattr(request, "resolver_match", None)
    route = getattr(match, "route", None) if match is not None else None
    if route:
        return ev.template_route(route)
    return ev.normalize_path(request.path)


def _user(request) -> None:
    user = getattr(request, "user", None)
    try:
        if user is not None and user.is_authenticated:
            set_user(user.pk)
    except Exception:  # noqa: BLE001 — custom user models, lazy objects
        pass


class CodeskopMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        try:
            import django

            set_framework(f"django {django.get_version()}")
        except Exception:  # noqa: BLE001
            set_framework("django")

    def __call__(self, request):
        recorder = RequestRecorder(request.method, request_id_from(request.headers.get))
        request._codeskop = recorder
        if trust_check(request, recorder, headers_get=request.headers.get, query_get=request.GET.get,
                       peer=request.META.get("REMOTE_ADDR")):
            from django.http import HttpResponse

            response = HttpResponse(BLOCKED_BODY, status=403, content_type="application/json")
            recorder.finish(ev.normalize_path(request.path), 403)
            return response
        response = self.get_response(request)
        _user(request)
        recorder.finish(_route(request), response.status_code,
                        int_or_none(request.META.get("CONTENT_LENGTH")),
                        int_or_none(response.get("Content-Length")) if hasattr(response, "get") else None)
        return response

    def process_exception(self, request, exception):
        recorder = getattr(request, "_codeskop", None)
        if recorder is not None:
            _user(request)
            recorder.exception(exception, _route(request), "django")
        return None
