# Changelog

## 0.1.2 (2026-10-22)

- README rewrite and package metadata (homepage https://www.codeskop.com). No code changes.
- PyPI classifier is now `Development Status :: 5 - Production/Stable`.

## 0.1.1 — 2026-10-06

- Contact email is now support@codeskop.com.

## 0.1.0 (beta) — 2026-10-05

First release.

- Errors: unhandled exceptions (requests, threads, main thread, Celery), `capture_exception`, `capture_message`, `LoggingHandler`. Chained causes; frames marked `in_app` so grouping follows your code.
- Incoming requests (`http_request`) per route template for Django, Flask and FastAPI/Starlette.
- Outgoing `requests` and `httpx` calls (`api_timing` / `api_error`).
- Background sending: batches of up to 100, gzip, `Retry-After` and exponential backoff, fork-safe, flush at exit.
- Remote config: kill switch, network feature gate, per-type sampling (failures are never sampled out).
- Zero runtime dependencies; Python 3.9–3.13.
