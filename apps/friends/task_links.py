"""Local-task ↔ Mission linkage (design §2.9).

A member's contribution to a Mission stays as their OWN local ``journal.Task``,
linked by ``Task.related_ref`` — zero schema change to journal.Task. When such a
Task is completed, we append a ``SharedGoalUpdate(task_completed)`` to the
mission's append-only stream (which feeds the projection + digest + envelope).

The seam is a single ``post_save`` receiver on ``journal.Task`` (covers the
lifecycle view, the runtime task-complete, and any other completion path). It is
DEFENSIVE (never raises into a save) and IDEMPOTENT (one task_completed per task,
so a re-save of an already-done task is a no-op) and fires ONLY when related_ref
points at a mission.
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_save

logger = logging.getLogger(__name__)


def _on_task_saved(sender, instance, **kwargs) -> None:
    try:
        related = getattr(instance, "related_ref", None) or {}
        if not isinstance(related, dict):
            return
        if related.get("pillar") != "friends" or related.get("object_type") != "shared_goal":
            return
        if instance.status != "done":
            return
        mission_id = related.get("object_id")
        if not mission_id:
            return

        from . import access
        from .models import SharedGoalUpdate

        mission = access.get_mission(mission_id)
        if mission is None:
            return
        already = SharedGoalUpdate.objects.filter(
            shared_goal=mission,
            kind=SharedGoalUpdate.Kind.TASK_COMPLETED,
            payload__task_id=str(instance.id),
        ).exists()
        if already:
            return  # idempotent — one completion update per task
        SharedGoalUpdate.objects.create(
            shared_goal=mission,
            tenant=instance.tenant,
            user=None,
            kind=SharedGoalUpdate.Kind.TASK_COMPLETED,
            text=(instance.title or "")[:200],
            payload={"task_id": str(instance.id), "title": instance.title or ""},
        )
    except Exception:  # noqa: BLE001 — never break a Task save over crew bookkeeping
        logger.warning("mission task-completion linkage failed", exc_info=True)


def connect() -> None:
    """Wire the receiver. Called from apps.FriendsConfig.ready()."""
    from apps.journal.models import Task

    post_save.connect(_on_task_saved, sender=Task, weak=False)
