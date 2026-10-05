"""Flask: `CodeskopFlask(app)` (or `CodeskopFlask().init_app(app)` with app factories).

Records every request by URL rule (`/orders/{id}`), captures unhandled
exceptions, and uses `flask_login.current_user` / `g.user_id` when present.
"""
from __future__ import annotations

from .. import events as ev
from ..client import set_framework, set_user
from . import BLOCKED_BODY, RequestRecorder, request_id_from, trust_check


class CodeskopFlask:
    def __init__(self, app=None):
        if app is not None:
            self.init_app(app)

    def init_app(self, app) -> None:
        import flask

        try:
            from importlib.metadata import version

            set_framework(f"flask {version('flask')}")
        except Exception:  # noqa: BLE001
            set_framework("flask")

        @app.before_request
        def _codeskop_start():
            req = flask.request
            recorder = RequestRecorder(req.method, request_id_from(req.headers.get))
            flask.g._codeskop = recorder
            if trust_check(req, recorder, headers_get=req.headers.get, query_get=req.args.get, peer=req.remote_addr):
                return flask.Response(BLOCKED_BODY, status=403, mimetype="application/json")
            return None

        @app.after_request
        def _codeskop_finish(response):
            recorder = flask.g.pop("_codeskop", None)
            if recorder is not None:
                _user(flask)
                recorder.finish(_route(flask.request), response.status_code, flask.request.content_length,
                                response.calculate_content_length() if hasattr(response, "calculate_content_length") else None)
            return response

        def _on_exception(sender, exception, **extra):
            recorder = flask.g.get("_codeskop")
            if recorder is None:
                return
            _user(flask)
            recorder.exception(exception, _route(flask.request), "flask")
            # Unhandled errors skip after_request; record the 500 here.
            recorder.finish(_route(flask.request), 500, flask.request.content_length)
            flask.g.pop("_codeskop", None)

        flask.got_request_exception.connect(_on_exception, app, weak=False)


def _route(request) -> str:
    rule = getattr(request, "url_rule", None)
    if rule is not None and getattr(rule, "rule", None):
        return ev.template_route(rule.rule)
    return ev.normalize_path(request.path)


def _user(flask) -> None:
    uid = getattr(flask.g, "user_id", None)
    if uid is None:
        try:
            from flask_login import current_user  # type: ignore

            if current_user and current_user.is_authenticated:
                uid = current_user.get_id()
        except Exception:  # noqa: BLE001
            uid = None
    if uid is not None:
        set_user(uid)
