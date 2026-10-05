# Codeskop for Python

Errors, incoming requests and outgoing API calls from your Python backend, in Codeskop.

- **Errors:** unhandled exceptions (web requests, threads, Celery tasks, the main thread), plus `capture_exception()`, `capture_message()` and a `logging` handler. Stack traces are grouped by your own code, not library frames.
- **Incoming requests:** every request your app handles, per route (`/orders/{pk}/`), with status, latency and size. Shown on the Network page under *Requests your servers receive*.
- **Outgoing calls:** `requests` and `httpx` calls to other APIs, with status, latency and failures.
- **Zero dependencies.** Python 3.9+. Never blocks a request: events are sent in the background, compressed and retried.

Status: beta (`0.1.0`).

## Install

```bash
pip install codeskop
```

## Set up

```python
import codeskop

codeskop.init(
    api_key="cs_live_pk_…",       # your project's public key (or set CODESKOP_API_KEY)
    environment="production",     # optional
    release="1.4.2",              # optional; auto-detected on Render, Heroku, Vercel, Railway, Cloud Run, GitHub Actions
)
```

### Django

```python
# settings.py
import codeskop
codeskop.init(api_key="cs_live_pk_…")

MIDDLEWARE = [
    "codeskop.integrations.django.CodeskopMiddleware",  # near the top
    # ...
]
```

### Flask

```python
from flask import Flask
import codeskop
from codeskop.integrations.flask import CodeskopFlask

codeskop.init(api_key="cs_live_pk_…")
app = Flask(__name__)
CodeskopFlask(app)          # or CodeskopFlask().init_app(app) in an app factory
```

### FastAPI / Starlette

```python
from fastapi import FastAPI
import codeskop
from codeskop.integrations.asgi import CodeskopMiddleware

codeskop.init(api_key="cs_live_pk_…")
app = FastAPI()
app.add_middleware(CodeskopMiddleware)
```

### Celery

```python
import codeskop
from codeskop.integrations import celery as codeskop_celery

codeskop.init(api_key="cs_live_pk_…")
codeskop_celery.install()
```

### Errors by hand

```python
try:
    charge(order)
except PaymentError as exc:
    codeskop.capture_exception(exc, tags={"provider": "stripe"})

codeskop.set_user(request.user.pk)         # attach later events to a user
logging.getLogger().addHandler(codeskop.LoggingHandler())   # ERROR logs → Codeskop
```

## Options

| Option | Default | |
|---|---|---|
| `api_key` | `CODESKOP_API_KEY` | Public key (`cs_live_pk_…` / `cs_test_pk_…`). Secret keys are refused. |
| `endpoint` | `https://api.codeskop.com` | `CODESKOP_ENDPOINT` |
| `environment` | `production` | `CODESKOP_ENVIRONMENT` |
| `release` | auto | `CODESKOP_RELEASE` |
| `capture_requests` | `True` | Incoming requests |
| `capture_outgoing` | `True` | `requests` / `httpx` calls |
| `ignore_routes` | `/health*`, `/healthz`, `/metrics`, `/favicon.ico` | Glob patterns not recorded |
| `ignore_exceptions` | `()` | Exception class names never sent |
| `before_send` | `None` | `fn(event) -> event | None` to edit or drop events |
| `send_user_id` | `True` | Attach the signed-in user's ID |
| `debug` | `False` | Log SDK activity |

## Privacy

Never captured: request or response bodies, cookies, `Authorization` headers, query strings. Codeskop also redacts payload fields named like secrets (password, token, api_key, card number…) on the server.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

The test suite runs against a local mock of the ingest API (`tests/conftest.py`). The contract is in the backend repo, `docs/10-server-sdk-spec.md`.

## License

MIT
