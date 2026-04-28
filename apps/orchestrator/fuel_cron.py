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

# Prefix for derived per-session crons. Distinct from the legacy
# ``_fuel:{plan_name}`` (which uses a plan name, not a UUID short-id) so the
# two paths can coexist during the cutover window.
_FUEL_SESSION_PREFIX = "_fuel:"


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


def regenerate_fuel_crons(tenant: Tenant) -> dict:
    """Reconcile the OpenClaw container's ``_fuel:*`` jobs with the derived set.

    Reads ``Workout.scheduled_at`` rows for the next 48h, compares against the
    container's current ``_fuel:*`` jobs (via the gateway), and applies the
    minimal diff: ``cron.add`` for missing, ``cron.remove`` for stale.

    This is the runtime arm of the source-of-truth inversion. Called from:
      - Workout post_save / post_delete signals (via debounced QStash task)
      - Hourly reconcile (catches drift)
      - Container-start hook (rebuilds after restart, replaces the legacy
        snapshot/restore path for ``_fuel:*``)

    Returns a dict with ``{added, removed, unchanged, errors}`` for telemetry.
    """
    from apps.cron.gateway_client import GatewayError, invoke_gateway_tool

    from .services import _extract_cron_jobs

    summary = {"added": 0, "removed": 0, "unchanged": 0, "errors": 0}

    profile = getattr(tenant, "fuel_profile", None)
    if not profile or not profile.use_session_scheduling:
        logger.debug("regenerate_fuel_crons: tenant %s not on new flow — skipping", tenant.id)
        return summary

    desired = derive_fuel_cron_jobs(tenant)
    desired_by_name: dict[str, dict] = {j["name"]: j for j in desired}

    try:
        list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
    except GatewayError:
        logger.exception("regenerate_fuel_crons: cron.list failed for tenant %s", tenant.id)
        summary["errors"] += 1
        return summary

    current_jobs = _extract_cron_jobs(list_result) or []
    current_fuel = {
        j.get("name", ""): j
        for j in current_jobs
        if isinstance(j, dict)
        and j.get("name", "").startswith(_FUEL_SESSION_PREFIX)
        # Exclude legacy `_fuel:{plan_name}` (which has a name with no UUID
        # short-id); only target derived per-session jobs (8 hex chars after
        # the prefix). Legacy emission is suppressed in build_cron_seed_jobs.
        and len(j.get("name", "").removeprefix(_FUEL_SESSION_PREFIX)) == 8
    }

    to_add = [desired_by_name[n] for n in desired_by_name if n not in current_fuel]
    to_remove = [current_fuel[n] for n in current_fuel if n not in desired_by_name]
    summary["unchanged"] = len(desired_by_name) - len(to_add)

    for job in to_add:
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

    for job in to_remove:
        job_id = job.get("id") or job.get("jobId", "")
        if not job_id:
            continue
        try:
            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
            summary["removed"] += 1
        except GatewayError:
            logger.warning(
                "regenerate_fuel_crons: cron.remove failed for %s on tenant %s",
                job_id[:12],
                tenant.id,
                exc_info=True,
            )
            summary["errors"] += 1

    logger.info(
        "regenerate_fuel_crons: tenant %s — added=%d removed=%d unchanged=%d errors=%d",
        str(tenant.id)[:8],
        summary["added"],
        summary["removed"],
        summary["unchanged"],
        summary["errors"],
    )
    return summary
