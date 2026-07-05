"""Mission status projection (design §7) — a PURE evidence builder.

Backend computes evidence, the LLM judges. Folds the append-only
``SharedGoalUpdate`` stream (control-plane only — NEVER a cross-tenant Task scan
in a request path) into one crew snapshot: per-member showed-up counts over the
target cadence window, streaks, last activity, next step, overall %. Mirrors the
shape conventions of ``apps.journal.status_projection.build_journal_status``.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .models import NeighborProfile, SharedGoalMembership, SharedGoalUpdate


def _handle_for(tenant_id) -> str | None:
    profile = NeighborProfile.objects.filter(tenant_id=tenant_id).only("handle").first()
    return profile.handle if profile else None


def _streak(days: set, today) -> int:
    """Consecutive days up to (and including) today that have a completion."""
    streak = 0
    cursor = today
    while cursor in days:
        streak += 1
        cursor = cursor - timedelta(days=1)
    return streak


def _next_step(updates: list, tenant_id) -> str | None:
    """The member's most recent ``task_added`` whose title hasn't since appeared
    in a ``task_completed`` — their open next step."""
    added = [u for u in updates if u.tenant_id == tenant_id and u.kind == SharedGoalUpdate.Kind.TASK_ADDED]
    completed_titles = {
        (u.payload or {}).get("title", "").strip().lower()
        for u in updates
        if u.tenant_id == tenant_id and u.kind == SharedGoalUpdate.Kind.TASK_COMPLETED
    }
    for update in reversed(added):
        title = (update.payload or {}).get("title") or update.text or ""
        if title.strip().lower() not in completed_titles:
            return title.strip()[:120] or None
    return None


def build_mission_status(mission, *, now=None) -> dict:
    """Fold the mission's update stream into a crew snapshot. ``mission`` is a
    ``SharedGoal`` instance (its ``.objects`` access already went through the
    accessor)."""
    now = now or timezone.now()
    target = mission.target or {}
    cadence = str(target.get("cadence", "daily")).lower()
    window_days = 28 if cadence == "weekly" else 7
    window_start = now - timedelta(days=window_days)

    updates = list(SharedGoalUpdate.objects.filter(shared_goal=mission).order_by("created_at"))
    memberships = list(
        SharedGoalMembership.objects.filter(shared_goal=mission, status="active").select_related("tenant")
    )

    members: list[dict] = []
    total_showed = 0
    for membership in memberships:
        completed = [
            u
            for u in updates
            if u.tenant_id == membership.tenant_id
            and u.kind == SharedGoalUpdate.Kind.TASK_COMPLETED
            and u.created_at >= window_start
        ]
        days = {u.created_at.date() for u in completed}
        showed_up = len(days)
        total_showed += showed_up
        last = max((u.created_at for u in updates if u.tenant_id == membership.tenant_id), default=None)
        members.append(
            {
                "handle": _handle_for(membership.tenant_id),
                "showed_up": showed_up,
                "window_days": window_days,
                "streak": _streak(days, now.date()),
                "last_activity": last.isoformat() if last else None,
                "next_step": _next_step(updates, membership.tenant_id),
                "commitment": membership.commitment,
                "is_creator": membership.role == "owner",
            }
        )

    total_possible = len(memberships) * window_days
    overall_pct = round(100 * total_showed / total_possible) if total_possible else 0

    return {
        "mission_id": str(mission.id),
        "title": mission.title,
        "status": mission.status,
        "cadence": cadence,
        "window_days": window_days,
        "target": target,
        "members": members,
        "overall_pct": overall_pct,
    }
