"""Journal → typed-row reconciliation.

Called by the extended nightly extraction (``run_extraction_for_tenant``)
on tenants with ``experimental_typed_journal_lifecycle`` on. Given the
day's journal evidence, the LLM proposes deltas against the tenant's
open ``Task`` rows and active ``Goal`` rows (complete, skip, defer,
in_progress, subtask_create, achieve, abandon). This module gathers
the LLM context, applies each delta safely (tenant-scoped, idempotent),
and records a ``PendingTaskAction`` row so the morning summary can
offer per-item Remove buttons that revert via ``before_state``.

The LLM never gets to mutate state directly — its output is always
validated tenant-side and applied through ``Task.complete()`` /
``Goal.mark_achieved()`` etc. so signals and ``updated_at`` fire
correctly.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from uuid import UUID

from apps.journal.models import Goal, PendingTaskAction, Task
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

MAX_TASKS_IN_CONTEXT = 50
MAX_GOALS_IN_CONTEXT = 25
EVIDENCE_MAX_CHARS = 1000


def gather_reconciliation_context(tenant: Tenant) -> dict[str, list[dict[str, Any]]]:
    """Return open tasks + active goals as compact JSON for the LLM.

    Each item carries the minimum fields the model needs to reason about
    matching ("does today's journal mention this?") and produce a delta:
    ``id`` for round-trip, ``title`` for matching, ``status``/``due_date``
    for context. Subtask relationships are preserved via
    ``parent_task_id`` so the model can address them precisely.
    """
    tasks = list(
        Task.objects.filter(
            tenant=tenant,
            status__in=[Task.Status.OPEN, Task.Status.IN_PROGRESS],
        )
        .order_by("due_date", "-updated_at")
        .values("id", "title", "status", "due_date", "parent_goal_id", "parent_task_id")[:MAX_TASKS_IN_CONTEXT]
    )
    goals = list(
        Goal.objects.filter(tenant=tenant, status=Goal.Status.ACTIVE)
        .order_by("target_date", "-updated_at")
        .values("id", "title", "status", "target_date")[:MAX_GOALS_IN_CONTEXT]
    )

    return {
        "open_tasks": [
            {
                "id": str(t["id"]),
                "title": t["title"],
                "status": t["status"],
                "due_date": t["due_date"].isoformat() if t["due_date"] else None,
                "parent_goal_id": str(t["parent_goal_id"]) if t["parent_goal_id"] else None,
                "parent_task_id": str(t["parent_task_id"]) if t["parent_task_id"] else None,
            }
            for t in tasks
        ],
        "active_goals": [
            {
                "id": str(g["id"]),
                "title": g["title"],
                "status": g["status"],
                "target_date": g["target_date"].isoformat() if g["target_date"] else None,
            }
            for g in goals
        ],
    }


def _coerce_uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        return None


def apply_task_action(
    *,
    tenant: Tenant,
    task_id: str,
    action: str,
    evidence: str,
    source_date: date,
) -> PendingTaskAction | None:
    """Apply a state delta to a single ``Task``, scoped to the tenant.

    Returns the recorded ``PendingTaskAction`` on success, ``None`` if
    the task doesn't exist, doesn't belong to the tenant, the action is
    unrecognised, or the task is already in the target state.
    """
    uuid = _coerce_uuid(task_id)
    if uuid is None:
        return None
    try:
        task = Task.objects.get(id=uuid, tenant=tenant)
    except Task.DoesNotExist:
        return None

    before_state = {
        "status": task.status,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }

    if action == "complete":
        if task.status == Task.Status.DONE:
            return None
        task.complete()
        kind = PendingTaskAction.Kind.TASK_COMPLETE
    elif action == "in_progress":
        if task.status == Task.Status.IN_PROGRESS:
            return None
        task.status = Task.Status.IN_PROGRESS
        task.save(update_fields=["status", "updated_at"])
        kind = PendingTaskAction.Kind.TASK_PROGRESS
    elif action == "skip":
        if task.status == Task.Status.SKIPPED:
            return None
        task.skip()
        kind = PendingTaskAction.Kind.TASK_SKIP
    elif action == "defer":
        if task.status == Task.Status.DEFERRED:
            return None
        task.defer()
        kind = PendingTaskAction.Kind.TASK_DEFER
    else:
        logger.warning("reconciliation: unknown task action %r for task %s", action, str(uuid)[:8])
        return None

    return PendingTaskAction.objects.create(
        tenant=tenant,
        kind=kind,
        task=task,
        evidence=evidence[:EVIDENCE_MAX_CHARS],
        source_date=source_date,
        before_state=before_state,
    )


def apply_subtask_create(
    *,
    tenant: Tenant,
    parent_task_id: str,
    title: str,
    source_date: date,
) -> PendingTaskAction | None:
    """Create a child Task under an existing parent, scoped to the tenant."""
    uuid = _coerce_uuid(parent_task_id)
    if uuid is None:
        return None
    try:
        parent = Task.objects.get(id=uuid, tenant=tenant)
    except Task.DoesNotExist:
        return None

    title = (title or "").strip()
    if not title:
        return None

    subtask = Task.objects.create(
        tenant=tenant,
        title=title[:256],
        parent_task=parent,
        parent_goal=parent.parent_goal,
        pillar=parent.pillar,
    )

    return PendingTaskAction.objects.create(
        tenant=tenant,
        kind=PendingTaskAction.Kind.SUBTASK_CREATE,
        task=subtask,
        evidence="",
        source_date=source_date,
        before_state={"created": True},
    )


def apply_goal_action(
    *,
    tenant: Tenant,
    goal_id: str,
    action: str,
    evidence: str,
    source_date: date,
) -> PendingTaskAction | None:
    """Apply a state delta to a single ``Goal``, scoped to the tenant."""
    uuid = _coerce_uuid(goal_id)
    if uuid is None:
        return None
    try:
        goal = Goal.objects.get(id=uuid, tenant=tenant)
    except Goal.DoesNotExist:
        return None

    before_state = {
        "status": goal.status,
        "achieved_at": goal.achieved_at.isoformat() if goal.achieved_at else None,
    }

    if action == "achieve":
        if goal.status == Goal.Status.ACHIEVED:
            return None
        goal.mark_achieved()
        kind = PendingTaskAction.Kind.GOAL_ACHIEVE
    elif action == "abandon":
        if goal.status == Goal.Status.ABANDONED:
            return None
        goal.abandon()
        kind = PendingTaskAction.Kind.GOAL_ABANDON
    else:
        logger.warning("reconciliation: unknown goal action %r for goal %s", action, str(uuid)[:8])
        return None

    return PendingTaskAction.objects.create(
        tenant=tenant,
        kind=kind,
        goal=goal,
        evidence=evidence[:EVIDENCE_MAX_CHARS],
        source_date=source_date,
        before_state=before_state,
    )


def apply_reconciliation_deltas(
    *,
    tenant: Tenant,
    deltas: dict[str, list[dict[str, Any]]],
    source_date: date,
) -> list[PendingTaskAction]:
    """Apply every delta in the LLM's reconciliation response.

    Order: task state changes first (so a parent task may be marked
    'in_progress' before a subtask is added under it); then subtask
    creates; then goal state changes. Skips any malformed entries
    silently — the LLM's output is untrusted.
    """
    actions: list[PendingTaskAction] = []

    for entry in deltas.get("task_updates", []) or []:
        if not isinstance(entry, dict):
            continue
        action = apply_task_action(
            tenant=tenant,
            task_id=str(entry.get("task_id") or ""),
            action=str(entry.get("action") or "").strip(),
            evidence=str(entry.get("evidence") or "").strip(),
            source_date=source_date,
        )
        if action:
            actions.append(action)

    for entry in deltas.get("subtasks_added", []) or []:
        if not isinstance(entry, dict):
            continue
        action = apply_subtask_create(
            tenant=tenant,
            parent_task_id=str(entry.get("parent_task_id") or ""),
            title=str(entry.get("title") or ""),
            source_date=source_date,
        )
        if action:
            actions.append(action)

    for entry in deltas.get("goal_updates", []) or []:
        if not isinstance(entry, dict):
            continue
        action = apply_goal_action(
            tenant=tenant,
            goal_id=str(entry.get("goal_id") or ""),
            action=str(entry.get("action") or "").strip(),
            evidence=str(entry.get("evidence") or "").strip(),
            source_date=source_date,
        )
        if action:
            actions.append(action)

    return actions


def undo_task_action(pending: PendingTaskAction) -> bool:
    """Reverse a previously-applied ``PendingTaskAction`` using ``before_state``.

    Returns True on success, False if the row referenced by the action no
    longer exists or the action is malformed.
    """
    before = pending.before_state or {}

    if pending.kind == PendingTaskAction.Kind.SUBTASK_CREATE:
        # PendingTaskAction.task uses on_delete=CASCADE, so we must drop
        # the FK before deleting the subtask — otherwise the cascade
        # removes the audit row and the caller's subsequent
        # status=UNDONE save fails with "Save with update_fields did not
        # affect any rows."
        if pending.task_id:
            from django.utils import timezone as _tz

            task_id = pending.task_id
            pending.task = None
            pending.status = PendingTaskAction.Status.UNDONE
            pending.resolved_at = _tz.now()
            pending.save(update_fields=["task", "status", "resolved_at"])
            Task.objects.filter(id=task_id, tenant=pending.tenant).delete()
        return True

    if pending.kind in (
        PendingTaskAction.Kind.TASK_COMPLETE,
        PendingTaskAction.Kind.TASK_PROGRESS,
        PendingTaskAction.Kind.TASK_SKIP,
        PendingTaskAction.Kind.TASK_DEFER,
    ):
        if not pending.task_id:
            return False
        try:
            task = Task.objects.get(id=pending.task_id, tenant=pending.tenant)
        except Task.DoesNotExist:
            return False
        from django.utils.dateparse import parse_datetime

        task.status = before.get("status", Task.Status.OPEN)
        ca = before.get("completed_at")
        task.completed_at = parse_datetime(ca) if ca else None
        task.save(update_fields=["status", "completed_at", "updated_at"])
        return True

    if pending.kind in (
        PendingTaskAction.Kind.GOAL_ACHIEVE,
        PendingTaskAction.Kind.GOAL_ABANDON,
    ):
        if not pending.goal_id:
            return False
        try:
            goal = Goal.objects.get(id=pending.goal_id, tenant=pending.tenant)
        except Goal.DoesNotExist:
            return False
        from django.utils.dateparse import parse_datetime

        goal.status = before.get("status", Goal.Status.ACTIVE)
        aa = before.get("achieved_at")
        goal.achieved_at = parse_datetime(aa) if aa else None
        goal.save(update_fields=["status", "achieved_at", "updated_at"])
        return True

    return False
