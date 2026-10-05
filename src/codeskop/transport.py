"""HTTP transport to the ingest API (docs/10 §10.4), standard library only.

`send()` never raises. It returns a `Result` telling the worker what to do with
the batch: done (sent or permanently refused) or retry after N seconds.
"""
from __future__ import annotations

import gzip
import json
import logging
import random
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from ._version import __version__

logger = logging.getLogger("codeskop")

MAX_BATCH_EVENTS = 100
MAX_EVENT_BYTES = 64 * 1024
MAX_BODY_BYTES = 1024 * 1024  # compressed
DEFAULT_RETRY_AFTER = 5.0
TIMEOUT = 10.0
USER_AGENT = f"codeskop-python/{__version__}"


@dataclass
class Result:
    done: bool
    retry_after: float = 0.0
    status: Optional[int] = None


def _retry_after(headers, default: float = DEFAULT_RETRY_AFTER) -> float:
    try:
        return max(0.0, float(headers.get("Retry-After", default)))
    except (TypeError, ValueError):
        return default


def backoff(attempt: int) -> float:
    """1, 2, 4 … 60 seconds with ±20 % jitter."""
    base = min(60.0, float(2 ** max(0, attempt)))
    return base * random.uniform(0.8, 1.2)


def compress(envelope: dict) -> bytes:
    return gzip.compress(json.dumps(envelope, separators=(",", ":"), default=str).encode("utf-8"))


class Transport:
    def __init__(self, endpoint: str, api_key: str):
        self.events_url = f"{endpoint}/v1/events"
        self.config_url = f"{endpoint}/v1/config"
        self.api_key = api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "User-Agent": USER_AGENT}

    def send(self, body: bytes, attempt: int = 0) -> Result:
        headers = {**self._headers(), "Content-Type": "application/json", "Content-Encoding": "gzip"}
        req = urllib.request.Request(self.events_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                status = resp.status
                raw = resp.read()
            if raw:
                try:
                    rejected = json.loads(raw).get("rejected")
                    if rejected:
                        logger.debug("codeskop: %d event(s) rejected by ingest", len(rejected))
                except (ValueError, AttributeError):
                    pass
            return Result(done=True, status=status)
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status in (429, 503):
                return Result(done=False, retry_after=_retry_after(exc.headers), status=status)
            if status >= 500:
                return Result(done=False, retry_after=backoff(attempt), status=status)
            logger.warning("codeskop: ingest refused a batch (HTTP %s); dropping it", status)
            return Result(done=True, status=status)
        except Exception as exc:  # noqa: BLE001 — network errors, timeouts
            logger.debug("codeskop: send failed (%s); will retry", exc)
            return Result(done=False, retry_after=backoff(attempt))

    def fetch_config(self, etag: Optional[str]) -> tuple:
        """(status, config | None, etag). status 304 means unchanged; None on failure."""
        headers = self._headers()
        if etag:
            headers["If-None-Match"] = etag
        req = urllib.request.Request(self.config_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), resp.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, None, etag
            return exc.code, None, etag
        except Exception:  # noqa: BLE001
            return None, None, etag

    def fetch_json(self, url: str, etag: Optional[str]) -> tuple:
        """GET any authenticated JSON resource (used for API Trust verdicts)."""
        headers = self._headers()
        if etag:
            headers["If-None-Match"] = etag
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), resp.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            return exc.code, None, etag
        except Exception:  # noqa: BLE001
            return None, None, etag
