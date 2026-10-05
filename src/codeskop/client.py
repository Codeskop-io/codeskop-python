"""The client: options, context, sampling, remote config and the background worker.

Nothing here blocks the caller. Events go into an in-memory queue; one daemon
thread batches, compresses and sends them, honouring Retry-After and backing off
on failures. After `os.fork()` the child process starts a fresh worker and queue
on first use (pre-fork servers such as gunicorn and uWSGI).
"""
from __future__ import annotations

import atexit
import contextvars
import json
import logging
import os
import platform
import random
import socket
import threading
import time
from collections import deque
from fnmatch import fnmatch
from typing import Any, Optional

from . import events as ev
from ._version import __version__
from .options import Options
from .transport import MAX_BATCH_EVENTS, MAX_BODY_BYTES, MAX_EVENT_BYTES, Transport, compress

logger = logging.getLogger("codeskop")

CONFIG_REFRESH_SECONDS = 300.0
_NEVER_SAMPLED = ("exception", "crash", "api_error")

_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("codeskop_user_id", default=None)
_framework: Optional[str] = None


def set_user(user_id: Optional[Any]) -> None:
    """Default user ID for events captured in the current request / task context."""
    _user_id.set(str(user_id) if user_id not in (None, "") else None)


def current_user() -> Optional[str]:
    return _user_id.get()


def set_framework(name: str) -> None:
    global _framework
    _framework = name


class Client:
    def __init__(self, options: Options):
        self.options = options
        self.enabled = options.enabled
        problem = options.key_problem()
        if problem:
            self.enabled = False
            logger.warning("codeskop: disabled: %s", problem)
        self.transport = Transport(options.endpoint, options.api_key)
        self.remote: dict = {}
        self._etag: Optional[str] = None
        self._config_at = 0.0
        self._closing = False
        self._reset_after_fork()
        try:
            os.register_at_fork(after_in_child=self._reset_after_fork)
        except AttributeError:  # pragma: no cover — Windows
            pass
        atexit.register(self.close)
        self.trust = None  # set by codeskop.trust when remote config enables API Trust

    # -- lifecycle -----------------------------------------------------------

    def _reset_after_fork(self) -> None:
        self._pid = os.getpid()
        self._queue: deque = deque()
        self._pending: Optional[list] = None  # a batch waiting to be retried
        self._attempt = 0
        self._next_send_at = 0.0
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._flush_requested = False

    def _ensure_worker(self) -> None:
        if os.getpid() != self._pid:
            self._reset_after_fork()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="codeskop-worker", daemon=True)
            self._thread.start()

    def close(self, timeout: Optional[float] = None) -> None:
        if not self.enabled or self._closing:
            return
        self.flush(self.options.shutdown_timeout if timeout is None else timeout)
        self._closing = True
        self._wake.set()

    def flush(self, timeout: float = 2.0) -> bool:
        """Block until everything queued is sent, or `timeout` passes."""
        if not self.enabled:
            return True
        with self._lock:
            empty = not self._queue and self._pending is None
        if empty:
            return True
        self._ensure_worker()
        self._next_send_at = 0.0
        self._flush_requested = True
        self._wake.set()
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                if not self._queue and self._pending is None:
                    return True
            time.sleep(0.02)
        return False

    # -- remote config & sampling -------------------------------------------

    def _refresh_config(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._config_at < CONFIG_REFRESH_SECONDS:
            return
        self._config_at = time.monotonic()
        status, config, etag = self.transport.fetch_config(self._etag)
        if status == 200 and isinstance(config, dict):
            self.remote, self._etag = config, etag
            if config.get("enabled") is False:
                with self._lock:
                    self._queue.clear()
                    self._pending = None
            if self.trust is None and isinstance(config.get("api_trust"), dict) and config["api_trust"].get("enabled"):
                from .trust import Trust

                self.trust = Trust(self)
            if self.trust is not None:
                self.trust.configure(config.get("api_trust") or {})
        elif status in (401, 403):
            logger.warning("codeskop: the API key was refused (HTTP %s); events won't be accepted", status)

    def feature(self, name: str, default: bool = True) -> bool:
        features = self.remote.get("features")
        return bool(features.get(name, default)) if isinstance(features, dict) else default

    def sample_rate(self, type_: str) -> float:
        rates = self.remote.get("sample_rates") if isinstance(self.remote.get("sample_rates"), dict) else {}
        local = self.options.sample_rates or {}
        for source in (rates, local):
            if type_ in source:
                return _rate(source[type_])
            if type_ == "http_request" and "api_timing" in source:
                return _rate(source["api_timing"])
        return 1.0

    def keep(self, event: dict) -> bool:
        if event["type"] in _NEVER_SAMPLED or event.get("severity") == "high":
            return True
        # API Trust is an audit trail: every attributed request is kept (docs/10 §10.9).
        if event["type"] == "http_request" and "consumer" in event.get("payload", {}):
            return True
        rate = self.sample_rate(event["type"])
        return rate >= 1.0 or random.random() < rate

    # -- capture -------------------------------------------------------------

    def context(self) -> dict:
        return {
            "device": {"platform": "python", "hostname": _HOSTNAME, "os": _OS, "runtime": _RUNTIME},
            "app": {k: v for k, v in {
                "release": self.options.release, "environment": self.options.environment,
                "framework": _framework, "sdk_name": "codeskop-python", "sdk_version": __version__,
            }.items() if v},
        }

    def capture(self, event: dict) -> None:
        if not self.enabled or self._closing or self.remote.get("enabled") is False:
            return
        try:
            if event["type"] in ("http_request", "api_timing", "api_error") and not self.feature("network"):
                return
            if not self.keep(event):
                return
            if self.options.before_send is not None:
                event = self.options.before_send(event)
                if not event:
                    return
            self._ensure_worker()
            with self._lock:
                if len(self._queue) >= self.options.max_queue_events:
                    self._queue.popleft()
                self._queue.append(event)
                full = len(self._queue) >= MAX_BATCH_EVENTS
            if full:
                self._wake.set()
        except Exception:  # noqa: BLE001 — never break the host app
            logger.debug("codeskop: capture failed", exc_info=True)

    def ignored_route(self, route: str) -> bool:
        return any(fnmatch(route, pattern) for pattern in self.options.ignore_routes)

    def ignored_exception(self, exc: BaseException) -> bool:
        if not self.options.ignore_exceptions:
            return False
        names = {type(exc).__name__, ev.exception_class_name(exc)}
        return any(name in names for name in self.options.ignore_exceptions)

    # -- worker --------------------------------------------------------------

    def _run(self) -> None:
        self._refresh_config(force=True)
        while True:
            self._wake.wait(timeout=self.options.flush_interval)
            self._wake.clear()
            try:
                self._refresh_config()
                self._drain(force=self._closing or self._flush_requested)
            except Exception:  # noqa: BLE001
                logger.debug("codeskop: worker loop error", exc_info=True)
            self._flush_requested = False
            if self._closing:
                return

    def _drain(self, force: bool) -> None:
        """Send queued batches. A failed batch stays pending and is retried as a whole
        (the server drops duplicate event IDs, so a partly sent batch is safe to resend)."""
        while True:
            if not force and time.monotonic() < self._next_send_at:
                return
            with self._lock:
                if self._pending is None:
                    batch = [self._queue.popleft() for _ in range(min(MAX_BATCH_EVENTS, len(self._queue)))]
                    if not batch:
                        return
                    self._pending, self._attempt = batch, 0
                batch = self._pending
            failure = None
            for body in self._bodies(batch):
                result = self.transport.send(body, self._attempt)
                if not result.done:
                    failure = result
                    break
            if failure is None:
                with self._lock:
                    self._pending = None
                    self._attempt = 0
                continue
            self._attempt += 1
            self._next_send_at = time.monotonic() + failure.retry_after
            if force and not self._closing and self._attempt < 3:
                time.sleep(min(failure.retry_after, 1.0))
                continue
            return

    def _bodies(self, batch: list):
        """Compressed request bodies for a batch, splitting anything over 1 MB."""
        events = []
        for event in batch:
            size = len(json.dumps(event, default=str))
            if size > MAX_EVENT_BYTES:
                logger.debug("codeskop: dropping a %d-byte event (limit 64 KB)", size)
                continue
            events.append(event)
        if not events:
            return []
        return self._split(events)

    def _split(self, events: list) -> list:
        body = compress({"sent_at": ev.now_iso(), "context": self.context(), "batch": events})
        if len(body) <= MAX_BODY_BYTES or len(events) == 1:
            return [body]
        mid = len(events) // 2
        return self._split(events[:mid]) + self._split(events[mid:])


def _rate(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


_HOSTNAME = socket.gethostname()
_OS = f"{platform.system()} {platform.release()}".strip()
_RUNTIME = f"{platform.python_implementation()} {platform.python_version()}"
