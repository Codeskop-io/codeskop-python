"""Celery: call `codeskop.integrations.celery.install()` after `codeskop.init(...)` in your worker.

Captures task failures (with the task name as a tag) and flushes after each task
so short-lived workers don't lose events.
"""
from __future__ import annotations

from .. import capture_exception, flush
from ..client import set_framework

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    from celery import signals

    try:
        import celery

        set_framework(f"celery {celery.__version__}")
    except Exception:  # noqa: BLE001
        set_framework("celery")

    @signals.task_failure.connect(weak=False)
    def _on_failure(sender=None, task_id=None, exception=None, **kwargs):
        if exception is not None:
            name = getattr(sender, "name", None) or str(sender)
            capture_exception(exception, handled=False, mechanism="celery", tags={"task": name, "task_id": task_id})

    @signals.task_postrun.connect(weak=False)
    def _after(**kwargs):
        flush(1.0)

    _installed = True
