"""Derive and apply per-session Fuel crons from `Workout.scheduled_at`.

Source-of-truth inversion: `Workout` rows in Postgres are the schedule of record;
the OpenClaw cron registry is a derived view. ``derive_fuel_cron_jobs`` is the
pure function producing the desired set; ``regenerate_fuel_crons`` diffs against
what's in the container and applies the minimal change via the gateway.
"""

from __future__ import annotations

import logging
import zoneinfo
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Prefix for the entire Fuel cron namespace. Derived per-session crons are
# named ``_fuel:{8-hex}`` (a Workout UUID's first segment). The pre-cutover
# path emitted ``_fuel:{plan_name}`` under the same prefix; on a
# session-scheduling tenant those legacy jobs are stale duplicates and the
# reconciler owns (and reaps) the whole prefix — see ``plan_fuel_cron_reconcile``.
_FUEL_SESSION_PREFIX = "_fuel:"

# Lowercase hex digits — the exact alphabet of a derived session cron's
# 8-char suffix (``str(uuid).split("-")[0]``).
_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_session_cron_name(name: str) -> bool:
    """True only for a derived per-session cron name ``_fuel:{8-hex}``.

    The session reconciler mints names from a Workout UUID's first segment
    (8 lowercase hex). Used only to LABEL a reaped orphan as ``stale`` (an
    8-hex session cron no longer wanted) vs ``legacy`` (a ``_fuel:{plan_name}``
    job) for telemetry — the reap decision itself is purely "not in desired".
    """
    if not name.startswith(_FUEL_SESSION_PREFIX):
        return False
    suffix = name[len(_FUEL_SESSION_PREFIX) :]
    return len(suffix) == 8 and all(c in _HEX_DIGITS for c in suffix)


def _user_tz_name(tenant) -> str:
    return str(getattr(tenant.user, "timezone", "") or "UTC")


def derive_fuel_cron_jobs(
    tenant: Tenant,
    *,
    horizon_hours: int = 48,
    now: datetime | None = None,
) -> list[dict]:
    """Build one cron-job dict per planned Workout in the next ``horizon_hours``.

    Reads ``Workout`` rows where ``status=planned`` and ``scheduled_at`` is in
    ``[now, now+horizon]``. Emits a one-shot cron per session named
    ``_fuel:{short_id}``; date-specific cron expressions (``M H D MON *``) make
    each fire exactly once at ``scheduled_at``. The prompt is the existing
    silent workout-prep prompt — message-cadence redesign is a v2 concern.

    Pure function — no DB writes, no gateway calls. Returns ``[]`` when fuel
    is disabled or no sessions are scheduled in the window.
    """
    from apps.fuel.models import Workout, WorkoutStatus

    from .config_generator import _FUEL_WORKOUT_PREP_PROMPT, _build_cron_message

    if not getattr(tenant, "fuel_enabled", False):
        return []

    if now is None:
        now = datetime.now(tz=UTC)

    horizon_end = now + timedelta(hours=horizon_hours)

    sessions = Workout.objects.filter(
        tenant=tenant,
        status=WorkoutStatus.PLANNED,
        scheduled_at__gte=now,
        scheduled_at__lte=horizon_end,
    ).order_by("scheduled_at")

    tz_name = _user_tz_name(tenant)
    try:
        user_tz = zoneinfo.ZoneInfo(tz_name)
    except zoneinfo.ZoneInfoNotFoundError:
        logger.warning("Unknown tz %r on tenant %s — falling back to UTC", tz_name, tenant.id)
        user_tz = UTC
        tz_name = "UTC"

    jobs: list[dict] = []
    for w in sessions:
        if not w.scheduled_at:
            continue
        local = w.scheduled_at.astimezone(user_tz)
        expr = f"{local.minute} {local.hour} {local.day} {local.month} *"
        # First UUID segment is 8 hex chars — collision-free across a single
        # tenant's planned-window. Avoids cron names that exceed gateway limits.
        short = str(w.id).split("-")[0]
        name = f"_fuel:{short}"
        jobs.append(
            {
                "name": name,
                "schedule": {"kind": "cron", "expr": expr, "tz": tz_name},
                "sessionTarget": "isolated",
                "payload": {
                    "kind": "agentTurn",
                    "message": _build_cron_message(
                        _FUEL_WORKOUT_PREP_PROMPT,
                        name,
                        foreground=False,
                        tenant=tenant,
                    ),
                },
                "delivery": {"mode": "none"},
                "enabled": True,
            }
        )

    return jobs


# Removal reasons → summary counter keys, for telemetry that tells the
# duplicate-fuel-cron cleanup story at a glance (legacy_reaped=N etc.).
_REASON_KEY = {
    "duplicate": "duplicates_reaped",
    "legacy": "legacy_reaped",
    "stale": "stale_reaped",
}


def _empty_summary() -> dict:
    return {
        "added": 0,
        "removed": 0,
        "unchanged": 0,
        "errors": 0,
        "duplicates_reaped": 0,
        "legacy_reaped": 0,
        "stale_reaped": 0,
    }


def plan_fuel_cron_reconcile(desired_by_name: dict[str, dict], current_jobs: list[dict]) -> dict:
    """Pure planner for the ``_fuel:*`` namespace — flow-agnostic.

    The reconciler is the SOLE owner of every ``_fuel:*`` cron; the only
    legitimate jobs are those in ``desired_by_name`` (session: derived 8-hex
    crons; legacy: the single active-plan workout-prep cron). Returns the
    minimal gateway actions to converge::

        {
          "to_add":    [job, ...],              # desired crons not on the box
          "to_remove": [(job, reason), ...],    # reason ∈ duplicate|stale|legacy
          "unchanged": int,                     # desired names already present
        }

    Removal reasons (labels only — reaping is purely "name not in desired"):
      * ``duplicate`` — an extra copy of a desired name (boot race / parallel
        ``cron.add`` / additive legacy create). The newest copy (by
        ``createdAtMs``) is kept.
      * ``stale``     — an 8-hex session cron no longer in the desired window
        (its Workout was rescheduled, completed, or deleted).
      * ``legacy``    — a non-8-hex ``_fuel:{plan_name}`` orphan: a plan that
        is no longer the active one, or a rename that stranded the old name.
        Reaping these is what seals the duplicate-fuel-cron leak fleet-wide.

    Pure: no gateway calls, no DB writes — unit-testable in isolation.
    """
    by_name: dict[str, list[dict]] = {}
    for j in current_jobs:
        if not isinstance(j, dict):
            continue
        name = j.get("name", "")
        if name.startswith(_FUEL_SESSION_PREFIX):
            by_name.setdefault(name, []).append(j)

    to_remove: list[tuple[dict, str]] = []
    present_desired: set[str] = set()

    for name, group in by_name.items():
        # Newest-first so the kept copy is the most recent — an agent/boot
        # refresh is likelier current than a stale pre-cutover entry. Missing
        # ``createdAtMs`` sorts as oldest.
        group.sort(key=lambda j: j.get("createdAtMs") or 0, reverse=True)
        if name in desired_by_name:
            present_desired.add(name)
            for dupe in group[1:]:
                to_remove.append((dupe, "duplicate"))
        else:
            reason = "stale" if _is_session_cron_name(name) else "legacy"
            for job in group:
                to_remove.append((job, reason))

    to_add = [desired_by_name[n] for n in desired_by_name if n not in present_desired]
    return {"to_add": to_add, "to_remove": to_remove, "unchanged": len(present_desired)}


def _desired_fuel_crons(tenant: Tenant) -> list[dict]:
    """The desired ``_fuel:*`` cron set for a tenant, by flow.

    * Fuel disabled        → ``[]`` (the reconciler reaps any leftover crons).
    * Session scheduling on → one derived cron per planned Workout in the 48h
      window (``derive_fuel_cron_jobs``).
    * Legacy (session off)  → at most ONE workout-prep cron, for the
      most-recent active plan — byte-for-byte what ``build_cron_seed_jobs``
      emits (apps/orchestrator/config_generator.py:build_fuel_workout_cron).
      This is the canonical legacy state; every other ``_fuel:*`` job (the
      additive-create / rename orphans that accumulated) is stale.
    """
    if not getattr(tenant, "fuel_enabled", False):
        return []

    profile = getattr(tenant, "fuel_profile", None)
    if profile and profile.use_session_scheduling:
        return derive_fuel_cron_jobs(tenant)

    from apps.fuel.models import WorkoutPlan

    from .config_generator import build_fuel_workout_cron

    plan = WorkoutPlan.objects.filter(tenant=tenant, status="active").order_by("-created_at").first()
    if plan is None:
        return []
    pref_time = getattr(profile, "preferred_time", "") if profile else ""
    job = build_fuel_workout_cron(tenant, plan, preferred_time=pref_time)
    return [job] if job else []


def regenerate_fuel_crons(tenant: Tenant) -> dict:
    """Reconcile the container's ``_fuel:*`` jobs with the desired set, for BOTH
    the session and legacy flows.

    ``_desired_fuel_crons`` produces the desired set (session: per-Workout 8-hex
    crons; legacy: the single most-recent-active-plan workout-prep cron; fuel
    off: none). This function diffs it against the container's current
    ``_fuel:*`` jobs and converges the WHOLE namespace via the minimal change:
    ``cron.add`` for missing, ``cron.remove`` for same-name duplicates, stale
    8-hex sessions, AND orphaned legacy ``_fuel:{plan_name}`` jobs. This is the
    single owner of the ``_fuel:*`` prefix — the general reconciler
    (cron_reconcile.py) deliberately skips it. See ``plan_fuel_cron_reconcile``.

    Called from:
      - Workout post_save / post_delete signals (session flow; debounced task)
      - Hourly reconcile across ALL fuel-enabled tenants (drift + duplicate
        backstop — this is what seals the accumulation fleet-wide)
      - Container-start hook (rebuilds ``_fuel:*`` after a restart)
      - The ``cleanup_fuel_crons`` management command

    Returns ``{added, removed, unchanged, errors, duplicates_reaped,
    legacy_reaped, stale_reaped}`` for telemetry.
    """
    from apps.cron.gateway_client import GatewayError, invoke_gateway_tool

    from .services import _extract_cron_jobs

    summary = _empty_summary()

    # Hibernated (scale 0/0) or suspended containers can't serve gateway
    # calls. The per-tenant task guards this, but the hourly fleet reconcile
    # (reconcile_fuel_crons_task) and any other caller must not slip through:
    # a ``cron.list`` POST to a scaled-to-zero container raises GatewayError
    # (a full stack trace + inflated error telemetry every hour) and can
    # cold-start an idle container, undermining hibernation. Reconcile-on-wake
    # rebuilds these crons, so skipping here loses nothing.
    from apps.tenants.models import Tenant

    if getattr(tenant, "hibernated_at", None) is not None or tenant.status == Tenant.Status.SUSPENDED:
        logger.info(
            "regenerate_fuel_crons: skipping %s (hibernated/suspended)",
            str(tenant.id)[:8],
        )
        return summary

    desired = _desired_fuel_crons(tenant)
    desired_by_name: dict[str, dict] = {j["name"]: j for j in desired}

    try:
        list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
    except GatewayError:
        logger.exception("regenerate_fuel_crons: cron.list failed for tenant %s", tenant.id)
        summary["errors"] += 1
        return summary

    current_jobs = _extract_cron_jobs(list_result) or []
    plan = plan_fuel_cron_reconcile(desired_by_name, current_jobs)
    summary["unchanged"] = plan["unchanged"]

    for job in plan["to_add"]:
        try:
            invoke_gateway_tool(tenant, "cron.add", {"job": job})
            summary["added"] += 1
        except GatewayError:
            logger.warning(
                "regenerate_fuel_crons: cron.add failed for %s on tenant %s",
                job["name"],
                tenant.id,
                exc_info=True,
            )
            summary["errors"] += 1

    for job, reason in plan["to_remove"]:
        # Fall back to the cron name as the remove key: the gateway's
        # cron.remove accepts a name as jobId, and a legacy ``_fuel:{plan_name}``
        # orphan that somehow lacks an id would otherwise be reaped by the
        # planner but silently skipped here (matches the sweep in
        # runtime_views._manage_fuel_cron and the general dedup reaper).
        job_id = job.get("id") or job.get("jobId") or job.get("name", "")
        if not job_id:
            continue
        try:
            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
            summary["removed"] += 1
            summary[_REASON_KEY[reason]] += 1
        except GatewayError:
            logger.warning(
                "regenerate_fuel_crons: cron.remove failed for %s (%s) on tenant %s",
                str(job_id)[:12],
                reason,
                tenant.id,
                exc_info=True,
            )
            summary["errors"] += 1

    logger.info(
        "regenerate_fuel_crons: tenant %s — added=%d removed=%d (dup=%d legacy=%d stale=%d) unchanged=%d errors=%d",
        str(tenant.id)[:8],
        summary["added"],
        summary["removed"],
        summary["duplicates_reaped"],
        summary["legacy_reaped"],
        summary["stale_reaped"],
        summary["unchanged"],
        summary["errors"],
    )
    return summary
