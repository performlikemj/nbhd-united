"""QStash-callable task functions for the friends app."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def scrub_shared_lesson_task(shared_lesson_id: str, pending_share_id: str | None = None) -> dict:
    """Async, fail-closed scrub of a SharedLesson snapshot (design §4.2).

    Runs on a WARM QStash worker — NEVER inline in a request (the 554MB DeBERTa
    cold-load must not sit in an HTTP handler). Returns a small status dict that
    carries no lesson content (task return values surface in QStash dashboards).
    """
    from apps.friends.scrub import scrub_shared_lesson

    result = scrub_shared_lesson(shared_lesson_id, pending_share_id=pending_share_id)
    logger.info("scrub_shared_lesson_task %s → %s", str(shared_lesson_id)[:8], result.get("reason"))
    return result
