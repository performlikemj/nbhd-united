"""Async tasks for the assistant baseline / insights subsystem.

All platform-level (iterate-all-tenants) tasks run synchronously under
``service_role=True`` RLS context — ``apps/cron/views.py:trigger_task`` sets
that before invoking us.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from apps.tenants.models import Tenant

from .models import PillarSnapshot
from .pillars import Pillar
from .snapshots import compute_gravity_snapshot

logger = logging.getLogger(__name__)


def _iso_week_bounds(at: datetime) -> tuple[datetime, datetime]:
    """Return [start, end) for the ISO week containing ``at`` (UTC, Mon→Mon)."""
    year, week_num, _ = at.isocalendar()
    week_start = datetime.fromisocalendar(year, week_num, 1).replace(tzinfo=UTC)
    return week_start, week_start + timedelta(days=7)


def _eligible_finance_tenants():
    """Tenants we should snapshot for Gravity this week.

    Skips hibernated tenants — waking them just for a snapshot would trigger
    an image-refresh cascade with cost, and we'd rather accept history gaps
    than thrash the fleet weekly.
    """
    return Tenant.objects.filter(
        finance_enabled=True,
        status=Tenant.Status.ACTIVE,
        hibernated_at__isnull=True,
    )


def snapshot_gravity_weekly_task() -> dict[str, int]:
    """Write a weekly Gravity ``PillarSnapshot`` for every eligible tenant.

    Idempotent: if a weekly snapshot already exists for the current ISO week
    for a tenant, it's skipped (so re-running the cron mid-week is safe).

    Returns a dict of {written, skipped_existing, errored} counts.
    """
    now = datetime.now(UTC)
    week_start, week_end = _iso_week_bounds(now)

    written = 0
    skipped = 0
    errored = 0
    for tenant in _eligible_finance_tenants().iterator():
        try:
            already = PillarSnapshot.objects.filter(
                tenant=tenant,
                pillar=Pillar.GRAVITY.value,
                granularity=PillarSnapshot.Granularity.WEEKLY,
                ts__gte=week_start,
                ts__lt=week_end,
            ).exists()
            if already:
                skipped += 1
                continue
            payload = compute_gravity_snapshot(tenant)
            PillarSnapshot.objects.create(
                tenant=tenant,
                pillar=Pillar.GRAVITY.value,
                ts=now,
                granularity=PillarSnapshot.Granularity.WEEKLY,
                payload=payload,
            )
            written += 1
        except Exception:
            errored += 1
            logger.exception("snapshot_gravity_weekly_task failed for tenant %s", tenant.id)

    logger.info(
        "snapshot_gravity_weekly_task done: written=%d skipped=%d errored=%d",
        written,
        skipped,
        errored,
    )
    return {"written": written, "skipped_existing": skipped, "errored": errored}


# ── Phase 4: Weekly reflection synthesis ─────────────────────────────


def _is_sunday_morning_local(tenant: Tenant, *, now: datetime, hour: int = 9) -> bool:
    """True when ``now`` converted to the tenant's local time is Sunday at ``hour``:xx.

    The hourly QStash dispatcher fans out to every active tenant. This filter
    decides whether the tenant is in the firing window right now. We match on
    the hour only (not the minute) so the schedule has a 1-hour-wide acceptance
    window — the cron fires at :00, dispatch happens within that minute.
    """
    from zoneinfo import ZoneInfo

    tz_name = (str(getattr(tenant.user, "timezone", "") or "UTC")).strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)
    # weekday(): Monday=0 … Sunday=6
    return local.weekday() == 6 and local.hour == hour


def weekly_gravity_reflection_task() -> dict[str, int]:
    """Hourly dispatcher: run weekly synthesis for any tenant in their Sunday 09:00 slot.

    Iterates ``finance_enabled=True`` + ``status=ACTIVE`` tenants (including
    hibernated — synthesis is platform-side, doesn't need the container).
    For each tenant whose local time is Sunday 09:00, calls
    ``apps.insights.synthesis.generate_weekly_reflection``. That function is
    idempotent per ISO week via the ``Document(kind=WEEKLY)`` slug check, so
    duplicate dispatches across the hour are no-ops.

    Returns a counts dict so the QStash trigger log shows what happened.
    """
    from apps.insights.synthesis import generate_weekly_reflection

    now = datetime.now(UTC)
    counts = {
        "considered": 0,
        "fired": 0,
        "skipped_window": 0,
        "skipped_already_ran": 0,
        "skipped_finance_disabled": 0,
        "skipped_volume_silent": 0,
        "skipped_no_data": 0,
        "skipped_no_reflection": 0,
        "errored_llm": 0,
        "errored_other": 0,
    }
    tenants = Tenant.objects.filter(
        finance_enabled=True,
        status=Tenant.Status.ACTIVE,
    ).select_related("user")
    for tenant in tenants.iterator():
        counts["considered"] += 1
        if not _is_sunday_morning_local(tenant, now=now):
            counts["skipped_window"] += 1
            continue
        try:
            result = generate_weekly_reflection(tenant, now=now)
        except Exception:
            counts["errored_other"] += 1
            logger.exception("weekly_gravity_reflection_task failed for tenant %s", tenant.id)
            continue

        if result.skipped == "already_ran":
            counts["skipped_already_ran"] += 1
        elif result.skipped == "finance_disabled":
            counts["skipped_finance_disabled"] += 1
        elif result.skipped == "volume_silent":
            counts["skipped_volume_silent"] += 1
        elif result.skipped == "no_data":
            counts["skipped_no_data"] += 1
        elif result.skipped == "no_reflection":
            counts["skipped_no_reflection"] += 1
        elif result.skipped == "llm_error":
            counts["errored_llm"] += 1
        elif not result.skipped:
            counts["fired"] += 1
        else:
            counts["errored_other"] += 1

    logger.info("weekly_gravity_reflection_task done: %s", counts)
    return counts
