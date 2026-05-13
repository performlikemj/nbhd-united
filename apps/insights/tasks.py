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
