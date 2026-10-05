"""Codeskop server SDK for Python.

    import codeskop
    codeskop.init(api_key="cs_live_pk_…", environment="production")

Every public function is safe to call before `init` (it does nothing) and never
raises into your code.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Any, Optional

from . import events as _events
from ._version import __version__
from .client import Client, current_user, set_user
from .options import Options

__all__ = [
    "LoggingHandler",
    "__version__",
    "capture_exception",
    "capture_message",
    "close",
    "flush",
    "get_client",
    "init",
    "set_user",
]

logger = logging.getLogger("codeskop")
_client: Optional[Client] = None
_hooks_installed = False


def init(api_key: Optional[str] = None, **options: Any) -> Client:
    """Start the SDK. Calling it again replaces the previous configuration."""
    global _client
    if api_key is not None:
        options["api_key"] = api_key
    opts = Options.from_kwargs(**options)
    if opts.debug and not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s codeskop %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    if _client is not None:
        _client.close(timeout=0.5)
    _client = Client(opts)
    _install_hooks()
    if opts.capture_outgoing:
        from .integrations import http_clients

        http_clients.install()
    return _client


def get_client() -> Optional[Client]:
    return _client


def capture_exception(error: Optional[BaseException] = None, *, user_id: Optional[str] = None,
                      tags: Optional[dict] = None, handled: bool = True, mechanism: str = "manual",
                      request: Optional[dict] = None) -> None:
    """Record an error. With no argument, records the exception currently being handled."""
    client = _client
    if client is None or not client.enabled:
        return
    try:
        if error is None:
            error = sys.exc_info()[1]
            if error is None:
                return
        if client.ignored_exception(error) or getattr(error, "_codeskop_captured", False):
            return
        try:
            error._codeskop_captured = True  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — some exception types reject attributes
            pass
        uid = user_id or (current_user() if client.options.send_user_id else None)
        client.capture(_events.exception_event(error, handled=handled, mechanism=mechanism, request=request,
                                               user_id=uid, tags=tags))
    except Exception:  # noqa: BLE001
        logger.debug("codeskop: capture_exception failed", exc_info=True)


def capture_message(message: str, severity: str = "medium") -> None:
    client = _client
    if client is None or not client.enabled:
        return
    try:
        client.capture(_events.message_event(message, severity, current_user()))
    except Exception:  # noqa: BLE001
        logger.debug("codeskop: capture_message failed", exc_info=True)


def flush(timeout: float = 2.0) -> bool:
    return _client.flush(timeout) if _client is not None else True


def close(timeout: float = 2.0) -> None:
    if _client is not None:
        _client.close(timeout)


class LoggingHandler(logging.Handler):
    """Send ERROR (and above) log records to Codeskop: `logging.getLogger().addHandler(codeskop.LoggingHandler())`."""

    def __init__(self, level: int = logging.ERROR):
        super().__init__(level)

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("codeskop"):
            return
        try:
            if record.exc_info and record.exc_info[1] is not None:
                capture_exception(record.exc_info[1], mechanism="logging", tags={"logger": record.name})
            else:
                capture_message(record.getMessage(), "critical" if record.levelno >= logging.CRITICAL else "high")
        except Exception:  # noqa: BLE001
            pass


def _install_hooks() -> None:
    """Unhandled exceptions in the main thread and in other threads."""
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    previous = sys.excepthook

    def excepthook(exc_type, exc, tb):
        if exc is not None and not isinstance(exc, KeyboardInterrupt):
            capture_exception(exc, handled=False, mechanism="excepthook")
            flush(2.0)
        previous(exc_type, exc, tb)

    sys.excepthook = excepthook
    previous_thread_hook = threading.excepthook

    def thread_hook(args):
        if args.exc_value is not None and not isinstance(args.exc_value, SystemExit):
            capture_exception(args.exc_value, handled=False, mechanism="threading")
        previous_thread_hook(args)

    threading.excepthook = thread_hook
