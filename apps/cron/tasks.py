"""Cron lifecycle maintenance tasks (QStash-scheduled — never Celery).

Registered in ``TASK_MAP`` (apps/cron/views.py) and fired by a no-body QStash
publish to ``/api/cron/trigger/<name>/``.
"""

from __future__ import annotations

import logging
from datetime import UTC, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.cron.models import CronJob

logger = logging.getLogger(__name__)

# How long past its fire time a one-shot cron is left alone before being retired.
# The container owns firing; this row is bookkeeping. A tenant hibernated across
# the fire time may run the job late on wake, so a generous grace keeps a
# same-hour wake honest without leaving the name squatted for long.
AT_CRON_GRACE = timedelta(hours=1)


def expire_finished_at_crons_task() -> dict:
    """Retire one-shot ("at") crons whose fire time has passed.

    WHY THIS EXISTS: OpenClaw deletes an at-kind job from its own store when it
    fires (``deleteAfterRun``), but nothing tells Django — there is **no**
    container→control-plane feedback path for cron fires (the ``cron_changed``
    hook is an in-container cache manager and makes zero Django calls). So the
    Postgres row stayed ``enabled=True`` forever. Combined with what used to be
    an UNCONDITIONAL ``(tenant, name)`` uniqueness constraint, the name was
    squatted for good: a user asking for the same reminder twice got a 409 on the
    second ask ("remind me at 3pm to call Mom" worked once, never again).

    The constraint is now scoped to ``enabled=True`` (apps/cron/models.py), so
    retiring the row here is what actually frees the name. This sweep IS the
    backfill — the first run retires every already-spent row.

    Zero-arg by the QStash TASK_MAP contract. Idempotent: a second pass over the
    same rows matches nothing, because they are already disabled.

    NOT a container operation. The bulk ``.update()`` below is deliberate, not a
    micro-optimization:
      * a per-row ``.save()`` would fire the ``pre_save`` contract-baking signal
        and re-render ``data`` on a dead row, and ``post_save`` would schedule a
        push to the container;
      * there is nothing to push — the container already deleted its copy when the
        job fired. This UPDATE RE-SYNCS the mirror rather than desyncing it.
        (invariants §9 is about deleting rows the container still HOLDS.)
    """
    now = timezone.now()
    cutoff = now - AT_CRON_GRACE

    # ``managed=False`` is what create_typed_cron / create_freeform_cron stamp on
    # an at-kind schedule (services.py::_is_at_schedule). Deliberately NOT filtered
    # on creation_path: a freeform at-cron squats its name identically.
    candidates = CronJob.objects.filter(managed=False, enabled=True).values_list("id", "data")

    expired_ids: list[int] = []
    for cron_id, data in candidates:
        schedule = (data or {}).get("schedule")
        if not isinstance(schedule, dict) or schedule.get("kind") != "at":
            continue
        fire_at = parse_datetime(str(schedule.get("at") or ""))
        if fire_at is None:
            # A malformed schedule is a finding, not a crash. Leave the row alone
            # and say so, so a broken writer stays visible instead of being
            # silently retired.
            logger.warning(
                "expire_finished_at_crons: cron %s has a missing/unparseable 'at' schedule — skipped",
                cron_id,
            )
            continue
        if timezone.is_naive(fire_at):
            fire_at = fire_at.replace(tzinfo=UTC)
        if fire_at < cutoff:
            expired_ids.append(cron_id)

    if not expired_ids:
        logger.info("expire_finished_at_crons: nothing to retire")
        return {"expired": 0, "ids": []}

    expired = CronJob.objects.filter(id__in=expired_ids, enabled=True).update(enabled=False)
    logger.info("expire_finished_at_crons: retired %s spent at-cron(s) %s", expired, expired_ids)
    return {"expired": expired, "ids": expired_ids}
