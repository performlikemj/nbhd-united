"""
Helper to publish on-demand tasks via QStash.

Replaces Celery's .delay() for async task execution. QStash sends an HTTP
POST to the cron trigger endpoint, which executes the task synchronously.
QStash handles retries natively (3 retries by default).

For fan-out patterns (e.g. apply-pending-configs iterating all tenants),
use ``publish_batch()`` to send all tasks in a single HTTP call instead
of N serial calls that block the Django worker.
"""
from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


def _publish_log_context(task_name: str, args, kwargs):
    context = {"task_name": task_name}
    if task_name == "provision_tenant" and args:
        context["tenant_id"] = str(args[0])
    if "tenant_id" in kwargs:
        context["tenant_id"] = str(kwargs["tenant_id"])
    if "user_id" in kwargs:
        context["user_id"] = str(kwargs["user_id"])
    return context


def publish_task(task_name: str, *args, idempotency_key: str | None = None, **kwargs):
    """
    Publish a one-off task to QStash for async execution.

    This replaces `some_task.delay(arg1, arg2)` with
    `publish_task("some_task", arg1, arg2)`.

    Args:
        task_name: URL-safe task name (must be in TASK_MAP).
        idempotency_key: Optional key for QStash deduplication. QStash will
            discard duplicate messages with the same key within a time window.
            Use this for broadcast-style tasks to prevent double delivery.
        *args, **kwargs: Arguments passed to the task function.
    """
    qstash_token = getattr(settings, "QSTASH_TOKEN", "")
    api_base_url = getattr(settings, "API_BASE_URL", "")
    log_context = _publish_log_context(task_name, args, kwargs)

    if not qstash_token or not api_base_url:
        # Fallback: execute synchronously (useful in development)
        logger.warning(
            "QStash not configured - executing task synchronously",
            extra=log_context,
        )
        from .views import TASK_MAP, execute_task_sync

        task_path = TASK_MAP[task_name]
        return execute_task_sync(task_path, *args, **kwargs)

    try:
        from qstash import QStash

        client = QStash(token=qstash_token)
        url = f"{api_base_url}/api/cron/trigger/{task_name}/"

        publish_kwargs: dict = {
            "url": url,
            "body": {"args": list(args), "kwargs": kwargs},
            "retries": 3,
        }
        if idempotency_key:
            publish_kwargs["deduplication_id"] = idempotency_key

        client.message.publish_json(**publish_kwargs)
        logger.info(
            "Published task to QStash",
            extra={**log_context, "url": url},
        )
    except Exception:
        logger.exception("Failed to publish task to QStash", extra=log_context)
        raise


def publish_batch(tasks: list[tuple[str, tuple, dict[str, Any]]]) -> int:
    """
    Publish multiple tasks to QStash in a single HTTP call.

    Each task is a tuple of (task_name, args, kwargs).  Uses QStash's
    ``batch_json`` API to avoid serial HTTP calls that block the Django
    worker.

    Returns the number of successfully enqueued tasks.

    Example::

        publish_batch([
            ("apply_single_tenant_config", (str(t.id),), {}),
            ("seed_cron_jobs", (str(t.id),), {}),
        ])
    """
    if not tasks:
        return 0

    qstash_token = getattr(settings, "QSTASH_TOKEN", "")
    api_base_url = getattr(settings, "API_BASE_URL", "")

    if not qstash_token or not api_base_url:
        logger.warning("QStash not configured - executing %d tasks synchronously", len(tasks))
        from .views import TASK_MAP, execute_task_sync

        count = 0
        for task_name, args, kwargs in tasks:
            try:
                task_path = TASK_MAP[task_name]
                execute_task_sync(task_path, *args, **kwargs)
                count += 1
            except Exception:
                logger.exception("Sync fallback failed for %s", task_name)
        return count

    try:
        from qstash import QStash

        client = QStash(token=qstash_token)
        messages = []
        for task_name, args, kwargs in tasks:
            url = f"{api_base_url}/api/cron/trigger/{task_name}/"
            messages.append({
                "url": url,
                "body": {"args": list(args), "kwargs": kwargs},
                "retries": 3,
            })

        results = client.message.batch_json(messages)
        logger.info("Batch published %d tasks to QStash", len(results))
        return len(results)
    except Exception:
        logger.exception("Failed to batch publish %d tasks to QStash", len(tasks))
        raise
