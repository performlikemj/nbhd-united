"""
Helper to publish on-demand tasks via QStash.

Replaces Celery's .delay() for async task execution. QStash sends an HTTP
POST to the cron trigger endpoint, which executes the task synchronously.
QStash handles retries natively (3 retries by default).
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def publish_task(task_name: str, *args, **kwargs):
    """
    Publish a one-off task to QStash for async execution.

    This replaces `some_task.delay(arg1, arg2)` with
    `publish_task("some_task", arg1, arg2)`.

    Args:
        task_name: URL-safe task name (must be in TASK_MAP).
        *args, **kwargs: Arguments passed to the task function.
    """
    qstash_token = getattr(settings, "QSTASH_TOKEN", "")
    api_base_url = getattr(settings, "API_BASE_URL", "")

    if not qstash_token or not api_base_url:
        # Fallback: execute synchronously (useful in development)
        logger.warning(
            "QStash not configured — executing task '%s' synchronously", task_name
        )
        from .views import execute_task_sync, TASK_MAP

        task_path = TASK_MAP[task_name]
        return execute_task_sync(task_path, *args, **kwargs)

    try:
        from qstash import QStash

        client = QStash(token=qstash_token)
        url = f"{api_base_url}/api/cron/trigger/{task_name}/"

        client.message.publish_json(
            url=url,
            body={"args": list(args), "kwargs": kwargs},
            retries=3,
        )
        logger.info("Published task '%s' to QStash -> %s", task_name, url)
    except Exception as e:
        logger.error("Failed to publish task '%s' to QStash: %s", task_name, e)
        raise
