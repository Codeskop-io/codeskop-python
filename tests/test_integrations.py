"""Framework integrations against the mock ingest: Django, Flask, Starlette/FastAPI, requests, httpx, Celery."""
from __future__ import annotations

import pytest

import codeskop

from .conftest import MockIngest

# ---------------------------------------------------------------------------
# Django
# ---------------------------------------------------------------------------

def _django():
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=False, SECRET_KEY="test", ALLOWED_HOSTS=["*"], ROOT_URLCONF="tests.django_urls",
            MIDDLEWARE=["codeskop.integrations.django.CodeskopMiddleware"], INSTALLED_APPS=[],
        )
        django.setup()


def test_django_requests_and_exceptions(sdk, ingest):
    pytest.importorskip("django")
    _django()
    from django.test import Client

    sdk()
    c = Client(raise_request_exception=False)
    assert c.get("/orders/42/").status_code == 200
    assert c.get("/orders/43/").status_code == 200
    assert c.get("/boom/").status_code == 500
    assert c.get("/healthz").status_code in (200, 404)
    c.get("/not-a-route/9999")
    codeskop.flush(3)
    reqs = sorted((e["payload"]["route"], e["payload"]["status"]) for e in ingest.of_type("http_request"))
    assert reqs == [("/boom/", 500), ("/not-a-route/{id}", 404), ("/orders/{pk}/", 200), ("/orders/{pk}/", 200)]
    [exc] = ingest.of_type("exception")
    assert exc["payload"]["mechanism"] == "django"
    assert exc["payload"]["request"]["route"] == "/boom/"
    assert ingest.batches[0]["context"]["app"]["framework"].startswith("django")


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------

def test_flask_requests_and_exceptions(sdk, ingest):
    flask = pytest.importorskip("flask")
    from codeskop.integrations.flask import CodeskopFlask

    app = flask.Flask(__name__)

    @app.route("/users/<int:user_id>")
    def user(user_id):
        flask.g.user_id = user_id
        return {"id": user_id}

    @app.route("/boom")
    def boom():
        raise ZeroDivisionError("nope")

    CodeskopFlask(app)
    sdk()
    client = app.test_client()
    assert client.get("/users/7").status_code == 200
    assert client.get("/boom").status_code == 500
    codeskop.flush(3)
    reqs = sorted((e["payload"]["route"], e["payload"]["status"]) for e in ingest.of_type("http_request"))
    assert reqs == [("/boom", 500), ("/users/{user_id}", 200)]
    assert [e.get("user") for e in ingest.of_type("http_request") if e["payload"]["status"] == 200] == [{"id": "7"}]
    [exc] = ingest.of_type("exception")
    assert (exc["payload"]["exception_class"], exc["payload"]["mechanism"]) == ("ZeroDivisionError", "flask")


# ---------------------------------------------------------------------------
# Starlette / FastAPI (ASGI)
# ---------------------------------------------------------------------------

def test_starlette_requests_and_exceptions(sdk, ingest):
    pytest.importorskip("starlette")
    pytest.importorskip("httpx")
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from codeskop.integrations.asgi import CodeskopMiddleware

    async def item(request):
        request.scope.setdefault("state", {})["user_id"] = "u-9"
        return JSONResponse({"id": request.path_params["item_id"]})

    async def fail(request):
        raise KeyError("missing")

    app = Starlette(routes=[Route("/items/{item_id}", item), Route("/fail", fail)])
    app.add_middleware(CodeskopMiddleware)
    sdk(capture_outgoing=False)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/items/5").status_code == 200
    assert client.get("/fail").status_code == 500
    codeskop.flush(3)
    reqs = sorted((e["payload"]["route"], e["payload"]["status"]) for e in ingest.of_type("http_request"))
    assert reqs == [("/fail", 500), ("/items/{item_id}", 200)]
    ok = next(e for e in ingest.of_type("http_request") if e["payload"]["status"] == 200)
    assert ok["user"] == {"id": "u-9"}
    assert ok["payload"]["response_bytes"] > 0
    assert ingest.of_type("exception")[0]["payload"]["mechanism"] == "asgi"


# ---------------------------------------------------------------------------
# Outgoing HTTP
# ---------------------------------------------------------------------------

@pytest.fixture
def upstream():
    mock = MockIngest()
    mock.extra_routes["/v1/charges/err"] = (502, {"detail": "bad gateway"})
    yield mock
    mock.close()


def test_requests_outgoing_calls(sdk, ingest, upstream):
    requests = pytest.importorskip("requests")
    sdk()
    target = upstream.url.replace("127.0.0.1", "localhost")  # a different host than the ingest endpoint
    requests.get(f"{target}/v1/customers/123?expand=1")
    requests.get(f"{target}/v1/charges/err")
    codeskop.flush(3)
    timings = sorted((e["payload"]["path"], e["payload"]["status"]) for e in ingest.of_type("api_timing"))
    assert timings == [("/v1/charges/err", 502), ("/v1/customers/{id}", 200)]
    [err] = ingest.of_type("api_error")
    assert (err["payload"]["error_kind"], err["payload"]["host"]) == ("http_5xx", "localhost")
    # The SDK's own calls to the ingest endpoint are never recorded.
    assert not [e for e in ingest.events if e["payload"].get("host") == "127.0.0.1"]


def test_httpx_outgoing_calls_and_network_errors(sdk, ingest, upstream):
    httpx = pytest.importorskip("httpx")
    sdk()
    target = upstream.url.replace("127.0.0.1", "localhost")
    httpx.get(f"{target}/v1/ping")
    with pytest.raises(httpx.ConnectError):
        httpx.get("http://localhost:1/unreachable")
    codeskop.flush(3)
    kinds = sorted((e["payload"]["path"], e["payload"].get("error_kind")) for e in ingest.of_type("api_error"))
    assert kinds == [("/unreachable", "network_error")]
    assert len(ingest.of_type("api_timing")) == 2


# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------

def test_celery_task_failure(sdk, ingest):
    celery = pytest.importorskip("celery")
    from codeskop.integrations import celery as codeskop_celery

    app = celery.Celery("t", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True

    @app.task(name="billing.charge")
    def charge():
        raise RuntimeError("card declined")

    sdk()
    codeskop_celery.install()
    charge.apply()
    codeskop.flush(3)
    [exc] = ingest.of_type("exception")
    assert exc["payload"]["mechanism"] == "celery"
    assert exc["payload"]["tags"]["task"] == "billing.charge"
