"""A mock Codeskop ingest server (docs/10 §10.10) and a fresh SDK per test."""
from __future__ import annotations

import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import codeskop

KEY = "cs_test_pk_abcdefgh12345678"
EVENT_TYPES = {"api_error", "api_timing", "exception", "crash", "crash_native", "anr", "heartbeat", "http_request",
               "track", "screen", "identify"}
SEVERITIES = {"low", "medium", "high", "critical"}


class MockIngest:
    def __init__(self):
        self.batches: list = []        # accepted envelopes
        self.requests: list = []       # (method, path, headers)
        self.responses: list = []      # scripted (status, headers) for POST /v1/events, consumed in order
        self.config: dict = {"enabled": True, "sample_rates": {}, "features": {"network": True}}
        self.config_status = 200
        self.extra_routes: dict = {}   # path -> (status, json)
        self.errors: list = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                mock.requests.append(("GET", self.path, dict(self.headers)))
                if self.path.startswith("/v1/config"):
                    return self._json(mock.config_status, mock.config, {"ETag": '"v1"'})
                for prefix, (status, body) in mock.extra_routes.items():
                    if self.path.startswith(prefix):
                        return self._json(status, body)
                return self._json(200, {"ok": True, "path": self.path})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                mock.requests.append(("POST", self.path, dict(self.headers)))
                if not self.path.startswith("/v1/events"):
                    return self._json(200, {"ok": True})
                if mock.responses:
                    status, headers = mock.responses.pop(0)
                    if status != 200:
                        return self._json(status, {"detail": "scripted"}, headers)
                try:
                    if self.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    envelope = json.loads(raw)
                    mock.validate(envelope, self.headers)
                except AssertionError as exc:
                    mock.errors.append(str(exc))
                    return self._json(400, {"errors": str(exc)})
                mock.batches.append(envelope)
                return self._json(200, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def validate(self, env, headers):
        """The same rules as apps/ingest/schemas.py plus the limits in views.py."""
        assert headers.get("Authorization") == f"Bearer {KEY}", "bad auth"
        assert isinstance(env.get("sent_at"), str), "sent_at"
        ctx = env.get("context")
        assert isinstance(ctx, dict) and isinstance(ctx.get("device"), dict) and isinstance(ctx.get("app"), dict), "context"
        batch = env.get("batch")
        assert isinstance(batch, list) and 1 <= len(batch) <= 100, "batch size"
        for e in batch:
            assert isinstance(e.get("event_id"), str), "event_id"
            assert e.get("type") in EVENT_TYPES, f"type {e.get('type')}"
            assert e.get("severity") in SEVERITIES, "severity"
            assert isinstance(e.get("occurred_at"), str) and e["occurred_at"].endswith("Z"), "occurred_at"
            assert isinstance(e.get("payload", {}), dict), "payload"
            assert len(json.dumps(e)) <= 64 * 1024, "event too large"

    @property
    def events(self) -> list:
        return [e for b in self.batches for e in b["batch"]]

    def of_type(self, type_: str) -> list:
        return [e for e in self.events if e["type"] == type_]

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def close(self):
        self.server.shutdown()


@pytest.fixture
def ingest():
    mock = MockIngest()
    yield mock
    mock.close()


@pytest.fixture
def sdk(ingest):
    """An initialised SDK pointed at the mock ingest; closed after the test."""
    def start(**options):
        options.setdefault("flush_interval", 0.05)
        client = codeskop.init(api_key=KEY, endpoint=ingest.url, **options)
        # Remote config is fetched by the worker on its first run.
        client._ensure_worker()
        ingest.wait_for(lambda: any(r[1].startswith("/v1/config") for r in ingest.requests), 2)
        time.sleep(0.05)
        return client
    yield start
    client = codeskop.get_client()
    if client is not None:
        client.close(0.5)
    codeskop._client = None
