"""API Trust capture in the SDK: consumer hashing, client signals, opt-in blocking."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

import codeskop

SALT = "s" * 64


def h(raw: str) -> str:
    return hmac.new(SALT.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]


def trust_config(blocking=False, sources=None):
    return {"enabled": True, "features": {"network": True}, "sample_rates": {},
            "api_trust": {"enabled": True, "salt": SALT, "trust_proxy": True, "blocking": blocking,
                          "verdicts_path": "/v1/trust/verdicts",
                          "consumer_sources": sources or [{"type": "header", "name": "X-API-Key"},
                                                          {"type": "jwt", "header": "Authorization",
                                                           "claims": ["client_id", "sub"]},
                                                          {"type": "mtls", "header": "X-Client-Cert"},
                                                          {"type": "query", "name": "api_key"}]}}


def _flask_app():
    flask = pytest.importorskip("flask")
    from codeskop.integrations.flask import CodeskopFlask

    app = flask.Flask("trust")

    @app.route("/v1/charges", methods=["POST", "GET"])
    def charges():
        return {"ok": True}

    CodeskopFlask(app)
    return app.test_client()


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def test_consumer_hashed_and_client_signals(sdk, ingest):
    ingest.config = trust_config()
    sdk()
    c = _flask_app()
    c.post("/v1/charges", headers={"X-API-Key": "live_secret_123", "X-Forwarded-For": "203.0.113.7, 10.0.0.1",
                                   "Origin": "https://shop.example.com", "User-Agent": "Shop/2.0 (com.shop.app)"})
    c.get("/v1/charges", headers={"Authorization": f"Bearer {jwt({'client_id': 'partner-42'})}"})
    c.get("/v1/charges", headers={"X-Client-Cert": "sha256:ab12"})
    c.get("/v1/charges?api_key=qkey")
    codeskop.flush(3)
    reqs = ingest.of_type("http_request")
    got = [(e["payload"]["consumer"]["auth_type"], e["payload"]["consumer"]["id_hash"]) for e in reqs]
    assert got == [("api_key", h("live_secret_123")), ("jwt", h("partner-42")),
                   ("mtls", h("sha256:ab12")), ("api_key", h("qkey"))]
    first = reqs[0]["payload"]["client"]
    assert (first["ip"], first["origin"], first["user_agent"]) == (
        "203.0.113.7", "https://shop.example.com", "Shop/2.0 (com.shop.app)")
    raw = json.dumps(ingest.events)
    assert "live_secret_123" not in raw and "partner-42" not in raw and "qkey" not in raw


def test_no_trust_fields_without_config(sdk, ingest):
    sdk()
    _flask_app().post("/v1/charges", headers={"X-API-Key": "k", "X-Forwarded-For": "203.0.113.7"})
    codeskop.flush(3)
    [e] = ingest.of_type("http_request")
    assert "consumer" not in e["payload"] and "client" not in e["payload"]


def test_blocking_is_opt_in_and_uses_verdicts(sdk, ingest):
    ingest.config = trust_config(blocking=True)
    ingest.extra_routes["/v1/trust/verdicts"] = (200, {"blocked": [h("banned-key")]})
    sdk()
    c = _flask_app()
    blocked = c.post("/v1/charges", headers={"X-API-Key": "banned-key"})
    allowed = c.post("/v1/charges", headers={"X-API-Key": "good-key"})
    assert (blocked.status_code, blocked.get_json()) == (403, {"error": "consumer_blocked"})
    assert allowed.status_code == 200
    codeskop.flush(3)
    flags = sorted((e["payload"]["status"], e["payload"].get("blocked", False)) for e in ingest.of_type("http_request"))
    assert flags == [(200, False), (403, True)]


def test_blocking_fails_open_when_verdicts_unavailable(sdk, ingest):
    ingest.config = trust_config(blocking=True)
    ingest.extra_routes["/v1/trust/verdicts"] = (503, {"detail": "down"})
    sdk()
    assert _flask_app().post("/v1/charges", headers={"X-API-Key": "banned-key"}).status_code == 200


def test_custom_resolver_and_asgi(sdk, ingest):
    pytest.importorskip("starlette")
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from codeskop.integrations.asgi import CodeskopMiddleware

    async def ep(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/v1/x", ep)])
    app.add_middleware(CodeskopMiddleware)
    ingest.config = trust_config()
    sdk(capture_outgoing=False, api_trust_resolver=lambda scope: dict(scope["headers"]).get(b"x-tenant", b"").decode() or None)
    TestClient(app).get("/v1/x", headers={"X-Tenant": "tenant-7", "X-Forwarded-For": "198.51.100.4"})
    codeskop.flush(3)
    [e] = ingest.of_type("http_request")
    assert e["payload"]["consumer"] == {"id_hash": h("tenant-7"), "auth_type": "custom", "source": "resolver"}
    assert e["payload"]["client"]["ip"] == "198.51.100.4"
