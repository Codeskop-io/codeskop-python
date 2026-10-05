"""Core client: keys, exceptions, transport, retries, batching, remote config, sampling, fork safety."""
from __future__ import annotations

import os
import sys
import threading

import pytest

import codeskop
from codeskop import events as ev
from codeskop.options import Options

from .conftest import KEY


def boom():
    raise ValueError("order total can't be negative")


def nested():
    try:
        boom()
    except ValueError as exc:
        raise RuntimeError("checkout failed") from exc


def test_secret_or_bad_key_disables_without_raising(ingest, caplog):
    client = codeskop.init(api_key="cs_live_sk_supersecret123456", endpoint=ingest.url)
    assert client.enabled is False
    assert "secret key" in caplog.text
    codeskop.capture_message("ignored")
    assert codeskop.flush(0.2)
    assert codeskop.init(api_key="nope", endpoint=ingest.url).enabled is False
    assert ingest.events == []
    codeskop._client = None


def test_options_from_environment(monkeypatch):
    monkeypatch.setenv("CODESKOP_API_KEY", KEY)
    monkeypatch.setenv("CODESKOP_ENDPOINT", "https://api-staging.codeskop.com/")
    monkeypatch.setenv("CODESKOP_ENVIRONMENT", "staging")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123")
    opts = Options.from_kwargs()
    assert (opts.api_key, opts.endpoint, opts.environment, opts.release) == (
        KEY, "https://api-staging.codeskop.com", "staging", "abc123")
    with pytest.raises(TypeError):
        Options.from_kwargs(not_an_option=True)


def test_exception_with_frames_cause_and_context(sdk, ingest):
    sdk(release="1.4.2", environment="test")
    codeskop.set_user(42)
    try:
        nested()
    except RuntimeError as exc:
        codeskop.capture_exception(exc, tags={"area": "checkout"})
    assert codeskop.flush(3)
    [event] = ingest.of_type("exception")
    p = event["payload"]
    assert (p["exception_class"], p["message"], p["handled"]) == ("RuntimeError", "checkout failed", True)
    assert p["stacktrace"][0]["method"] == "nested"  # innermost first
    assert p["stacktrace"][0]["class"] == "tests.test_core"
    assert p["stacktrace"][0]["in_app"] is True
    assert p["cause"]["exception_class"] == "ValueError"
    assert p["cause"]["stacktrace"][0]["method"] == "boom"
    assert p["tags"] == {"area": "checkout"}
    assert event["user"] == {"id": "42"}
    ctx = ingest.batches[0]["context"]
    assert ctx["device"]["platform"] == "python"
    assert (ctx["app"]["release"], ctx["app"]["environment"], ctx["app"]["sdk_name"]) == ("1.4.2", "test", "codeskop-python")


def test_library_frames_are_not_in_app():
    import json as stdlib_json

    try:
        stdlib_json.loads("{not json")
    except ValueError as exc:
        frames = ev.frames_from_traceback(exc.__traceback__)
    assert frames[-1]["in_app"] is True  # this test function
    assert any(not f["in_app"] for f in frames)  # json/decoder.py


def test_capture_exception_without_argument_and_dedup(sdk, ingest):
    sdk()
    try:
        boom()
    except ValueError as exc:
        codeskop.capture_exception()
        codeskop.capture_exception(exc)  # the same object isn't sent twice
    codeskop.flush(3)
    assert len(ingest.of_type("exception")) == 1


def test_ignore_exceptions_and_before_send(sdk, ingest):
    def scrub(event):
        if event["payload"].get("message") == "drop me":
            return None
        event["payload"]["message"] = "[scrubbed]"
        return event

    sdk(ignore_exceptions=("KeyError",), before_send=scrub)
    codeskop.capture_exception(KeyError("x"))
    codeskop.capture_message("drop me")
    codeskop.capture_message("secret thing")
    codeskop.flush(3)
    assert [e["payload"]["message"] for e in ingest.events] == ["[scrubbed]"]


def test_retry_after_429_then_success(sdk, ingest):
    sdk()
    ingest.responses = [(429, {"Retry-After": "0"}), (503, {"Retry-After": "0"})]
    codeskop.capture_message("eventually delivered")
    assert codeskop.flush(5)
    assert [e["payload"]["message"] for e in ingest.events] == ["eventually delivered"]
    posts = [r for r in ingest.requests if r[0] == "POST"]
    assert len(posts) == 3
    assert posts[0][2]["Content-Encoding"] == "gzip"


def test_permanent_4xx_drops_the_batch(sdk, ingest):
    sdk()
    ingest.responses = [(400, {})]
    codeskop.capture_message("bad")
    assert codeskop.flush(3)
    codeskop.capture_message("next")
    codeskop.flush(3)
    assert [e["payload"]["message"] for e in ingest.events] == ["next"]


def test_batches_of_at_most_100(sdk, ingest):
    sdk()
    for i in range(250):
        codeskop.capture_message(f"m{i}")
    assert codeskop.flush(5)
    sizes = [len(b["batch"]) for b in ingest.batches]
    assert max(sizes) <= 100 and sum(sizes) == 250 and len(sizes) >= 3
    assert len({e["event_id"] for e in ingest.events}) == 250


def test_remote_config_kill_switch(sdk, ingest):
    ingest.config = {"enabled": False}
    sdk()
    codeskop.capture_message("not sent")
    codeskop.flush(0.5)
    assert ingest.events == []


def test_sampling_never_drops_failures(sdk, ingest):
    ingest.config = {"enabled": True, "sample_rates": {"http_request": 0.0}, "features": {"network": True}}
    client = sdk()
    client.capture(ev.request_event(method="GET", route="/ok", status=200, duration_ms=5))
    client.capture(ev.request_event(method="GET", route="/broken", status=503, duration_ms=5))
    client.capture(ev.request_event(method="GET", route="/raised", status=200, duration_ms=5, failed=True))
    codeskop.flush(3)
    assert sorted(e["payload"]["route"] for e in ingest.of_type("http_request")) == ["/broken", "/raised"]


def test_network_feature_off_drops_request_events(sdk, ingest):
    ingest.config = {"enabled": True, "features": {"network": False}}
    client = sdk()
    client.capture(ev.request_event(method="GET", route="/x", status=500, duration_ms=5))
    codeskop.capture_message("errors still flow")
    codeskop.flush(3)
    assert [e["type"] for e in ingest.events] == ["exception"]


def test_unhandled_thread_exception(sdk, ingest):
    sdk()
    t = threading.Thread(target=boom)
    t.start()
    t.join()
    codeskop.flush(3)
    [event] = ingest.of_type("exception")
    assert (event["payload"]["mechanism"], event["payload"]["handled"], event["severity"]) == ("threading", False, "high")


def test_excepthook_chains(sdk, ingest, monkeypatch):
    sdk()
    try:
        boom()
    except ValueError:
        sys.excepthook(*sys.exc_info())
    codeskop.flush(3)
    assert ingest.of_type("exception")[0]["payload"]["mechanism"] == "excepthook"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork only")
def test_fork_child_gets_its_own_worker(sdk, ingest):
    sdk()
    codeskop.capture_message("parent")
    codeskop.flush(3)
    pid = os.fork()
    if pid == 0:  # child
        try:
            codeskop.capture_message("child")
            ok = codeskop.flush(5)
        finally:
            os._exit(0 if ok else 1)
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0
    assert sorted(e["payload"]["message"] for e in ingest.events) == ["child", "parent"]


def test_route_helpers():
    assert ev.template_route("orders/<int:pk>/items/<slug:item>/") == "/orders/{pk}/items/{item}/"
    assert ev.template_route("/users/<id>") == "/users/{id}"
    assert ev.template_route("^api/v1/$") == "/api/v1/"
    assert ev.normalize_path("/users/42/files/0f8fad5b-d9cb-469f-a165-70867728950e?x=1") == "/users/{id}/files/{id}"


def test_logging_handler(sdk, ingest):
    import logging

    sdk()
    log = logging.getLogger("shop.test")
    log.addHandler(codeskop.LoggingHandler())
    try:
        boom()
    except ValueError:
        log.exception("charging failed")
    log.error("plain error")
    log.warning("not sent")
    codeskop.flush(3)
    kinds = sorted((e["payload"]["exception_class"], e["severity"]) for e in ingest.events)
    assert kinds == [("Message", "high"), ("ValueError", "medium")]
