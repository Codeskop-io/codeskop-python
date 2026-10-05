"""SDK options (docs/10 §10.3), read from keyword arguments and environment variables."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

DEFAULT_ENDPOINT = "https://api.codeskop.com"
DEFAULT_IGNORE_ROUTES = ("/health*", "/healthz", "/metrics", "/favicon.ico")
PUBLIC_KEY_RE = re.compile(r"^cs_(live|test)_pk_[A-Za-z0-9_-]{8,}$")

# Hosting providers that expose the deployed commit in the environment.
_RELEASE_ENV_VARS = (
    "CODESKOP_RELEASE",
    "RENDER_GIT_COMMIT",
    "HEROKU_SLUG_COMMIT",
    "SOURCE_VERSION",
    "VERCEL_GIT_COMMIT_SHA",
    "RAILWAY_GIT_COMMIT_SHA",
    "K_REVISION",
    "GITHUB_SHA",
)


def _detect_release() -> Optional[str]:
    for name in _RELEASE_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value[:64]
    return None


@dataclass
class Options:
    api_key: str = ""
    endpoint: str = DEFAULT_ENDPOINT
    environment: str = "production"
    release: Optional[str] = None
    capture_requests: bool = True
    capture_outgoing: bool = True
    sample_rates: dict = field(default_factory=dict)
    ignore_routes: tuple = DEFAULT_IGNORE_ROUTES
    ignore_exceptions: tuple = ()
    before_send: Optional[Callable[[dict], Optional[dict]]] = None
    send_user_id: bool = True
    max_queue_events: int = 10_000
    flush_interval: float = 5.0
    shutdown_timeout: float = 2.0
    debug: bool = False
    enabled: bool = True
    # API Trust (only used when remote config enables it; docs/10 §10.9).
    api_trust_resolver: Optional[Callable[[Any], Optional[str]]] = None
    trust_proxy: Optional[bool] = None

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> Options:
        unknown = set(kwargs) - set(cls.__dataclass_fields__)
        if unknown:
            raise TypeError(f"Unknown Codeskop option(s): {', '.join(sorted(unknown))}")
        opts = cls(**kwargs)
        opts.api_key = (opts.api_key or os.environ.get("CODESKOP_API_KEY", "")).strip()
        if "endpoint" not in kwargs:
            opts.endpoint = os.environ.get("CODESKOP_ENDPOINT", DEFAULT_ENDPOINT)
        opts.endpoint = opts.endpoint.rstrip("/")
        if "environment" not in kwargs:
            opts.environment = os.environ.get("CODESKOP_ENVIRONMENT", "production")
        if opts.release is None:
            opts.release = _detect_release()
        opts.ignore_routes = tuple(opts.ignore_routes or ())
        opts.ignore_exceptions = tuple(opts.ignore_exceptions or ())
        return opts

    def key_problem(self) -> Optional[str]:
        """Why the key can't be used, or None when it looks like a public key."""
        if not self.api_key:
            return "no api_key (set CODESKOP_API_KEY or pass api_key=)"
        if "_sk_" in self.api_key:
            return "a secret key (_sk_) was given; use the project's public key (cs_..._pk_...)"
        if not PUBLIC_KEY_RE.match(self.api_key):
            return "the api_key doesn't look like a Codeskop public key (cs_live_pk_... or cs_test_pk_...)"
        return None
