"""API Trust capture (docs/10 §10.9), active only when remote config contains `api_trust`.

Adds the calling consumer (a salted hash of its API key, JWT client ID or client
certificate; the raw value never leaves your server) and client signals (IP,
User-Agent, Origin, Referer) to incoming-request events, and optionally blocks
consumers your team has blocked in Codeskop. Blocking fails open: if the verdict
list can't be fetched, nothing is blocked.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("codeskop")
VERDICT_REFRESH = 60.0


class Trust:
    def __init__(self, client):
        self.client = client
        self.active = False
        self.sources: list = []
        self.salt = ""
        self.trust_proxy = True
        self.blocking = False
        self.verdicts_path = "/v1/trust/verdicts"
        self._blocked: frozenset = frozenset()
        self._verdict_etag: Optional[str] = None
        self._verdicts_at = 0.0
        self._lock = threading.Lock()

    def configure(self, cfg: dict) -> None:
        self.active = bool(cfg.get("enabled")) and bool(cfg.get("salt"))
        self.sources = [s for s in cfg.get("consumer_sources") or [] if isinstance(s, dict)]
        self.salt = str(cfg.get("salt") or "")
        opt = self.client.options.trust_proxy
        self.trust_proxy = bool(cfg.get("trust_proxy", True)) if opt is None else bool(opt)
        self.blocking = bool(cfg.get("blocking"))
        self.verdicts_path = str(cfg.get("verdicts_path") or "/v1/trust/verdicts")
        if not self.blocking:
            self._blocked = frozenset()
        elif not self._verdicts_at:
            self._fetch_verdicts()  # called from the worker thread: load before the first request needs it

    # -- consumer identity ------------------------------------------------------

    def _hash(self, raw: str) -> str:
        return hmac.new(self.salt.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]

    def consumer(self, request: Any, headers_get: Callable, query_get: Callable) -> Optional[dict]:
        resolver = self.client.options.api_trust_resolver
        if resolver is not None:
            try:
                raw = resolver(request)
            except Exception:  # noqa: BLE001
                raw = None
            if raw:
                return {"id_hash": self._hash(str(raw)), "auth_type": "custom", "source": "resolver"}
        for src in self.sources:
            kind = src.get("type")
            raw, auth_type, label = None, "api_key", ""
            if kind == "header":
                value = headers_get(src.get("name", "")) or headers_get(str(src.get("name", "")).lower())
                scheme = src.get("scheme")
                if value and scheme:
                    prefix = f"{scheme} "
                    value = value[len(prefix):] if value.lower().startswith(prefix.lower()) else None
                raw, label = value, f"header:{src.get('name')}"
            elif kind == "query":
                raw, label = query_get(src.get("name", "")), f"query:{src.get('name')}"
            elif kind == "jwt":
                raw = _jwt_claim(headers_get(src.get("header", "Authorization")) or
                                 headers_get(str(src.get("header", "Authorization")).lower()), src.get("claims") or ["sub"])
                auth_type, label = "jwt", "jwt"
            elif kind == "mtls":
                raw = headers_get(src.get("header", "")) or headers_get(str(src.get("header", "")).lower())
                auth_type, label = "mtls", f"mtls:{src.get('header')}"
            if raw:
                return {"id_hash": self._hash(str(raw).strip()), "auth_type": auth_type, "source": label[:80]}
        return None

    # -- client signals -----------------------------------------------------------

    def client_ip(self, headers_get: Callable, peer: Optional[str]) -> Optional[str]:
        if self.trust_proxy:
            fwd = headers_get("x-forwarded-for") or headers_get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()[:64]
            fwd = headers_get("forwarded") or headers_get("Forwarded")
            if fwd and "for=" in fwd:
                part = fwd.split("for=", 1)[1].split(";", 1)[0].split(",", 1)[0].strip().strip('"')
                return part.strip("[]").split("]:")[0][:64]
            real = headers_get("x-real-ip") or headers_get("X-Real-Ip")
            if real:
                return real.strip()[:64]
        return peer

    def inspect(self, request: Any, *, headers_get: Callable, query_get: Callable, peer: Optional[str]) -> tuple:
        """(extra payload fields, blocked?) for one incoming request."""
        def h(name):
            try:
                return headers_get(name)
            except Exception:  # noqa: BLE001
                return None

        extra: dict = {}
        consumer = self.consumer(request, h, query_get)
        if consumer:
            extra["consumer"] = consumer
        client = {k: v for k, v in {
            "ip": self.client_ip(h, peer),
            "user_agent": (h("user-agent") or h("User-Agent") or "")[:300],
            "origin": (h("origin") or h("Origin") or "")[:300],
            "referer": (h("referer") or h("Referer") or "")[:300],
            "requested_with": (h("x-requested-with") or h("X-Requested-With") or "")[:200],
        }.items() if v}
        if client:
            extra["client"] = client
        blocked = bool(consumer and self.blocking and consumer["id_hash"] in self.blocked())
        return extra, blocked

    # -- verdicts -------------------------------------------------------------------

    def blocked(self) -> frozenset:
        if not self.blocking:
            return frozenset()
        if time.monotonic() - self._verdicts_at > VERDICT_REFRESH and self._lock.acquire(blocking=False):
            threading.Thread(target=self._refresh_verdicts, daemon=True).start()
        return self._blocked

    def _refresh_verdicts(self) -> None:
        try:
            self._fetch_verdicts()
        finally:
            self._lock.release()

    def _fetch_verdicts(self) -> None:
        try:
            self._verdicts_at = time.monotonic()
            url = self.client.options.endpoint + self.verdicts_path
            status, body, etag = self.client.transport.fetch_json(url, self._verdict_etag)
            if status == 200 and isinstance(body, dict):
                self._blocked = frozenset(str(x) for x in body.get("blocked") or [])
                self._verdict_etag = etag
        except Exception:  # noqa: BLE001 — fail open
            logger.debug("codeskop: verdict refresh failed", exc_info=True)


def _jwt_claim(header: Optional[str], claims: list) -> Optional[str]:
    """Read an identifying claim from a Bearer JWT without verifying it (identification only)."""
    if not header:
        return None
    token = header.split(" ", 1)[1] if header.lower().startswith("bearer ") else header
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception:  # noqa: BLE001
        return None
    for claim in claims:
        value = payload.get(claim) if isinstance(payload, dict) else None
        if value:
            return str(value)
    return None
