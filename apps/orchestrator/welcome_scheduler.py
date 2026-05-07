"""Shared welcome-cron scheduler for feature-toggle welcome flows.

Phase 1.3 makes welcome scheduling self-healing:
- Stale crons (date already passed) are detected and replaced.
- The result enum bubbles up to backfill telemetry.
- Helpers raise on transport failures so callers can distinguish
  "scheduled fresh" / "replaced stale" / "skipped (already delivered)" /
  "skipped (still pending)" / "failed".

The original Phase 1 implementation swallowed all exceptions inside
``_schedule_fuel_welcome`` / ``_schedule_finance_welcome`` and used a
binary ``cron_exists`` check. Both choices contributed to the canary
incident on 2026-05-07: a date-pattern one-shot fired on 2026-04-25 but
the agent crashed mid-turn (PII redactor import error). The cron was
never self-removed, ``cron_exists`` reported True (next fire = 2027),
and the swallow-all helpers reported "scheduled" to the backfill
counter even when the scheduling actually skipped or failed.
"""

from __future__ import annotations

import logging
import zoneinfo
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


class WelcomeStatus(StrEnum):
    SCHEDULED = "scheduled"
    REPLACED_STALE = "replaced_stale"
    SKIPPED_PENDING = "skipped_pending"
    SKIPPED_ALREADY_DELIVERED = "skipped_already_delivered"


# Welcomes are encoded as date-pattern crons (``M H D M *``) which are
# annual recurring. A "still pending" welcome should fire within the
# next day — anything beyond that is the annual-recurrence picking up
# because the original fire happened without successful self-removal.
_ONE_SHOT_WINDOW = timedelta(days=1)


def schedule_welcome(
    tenant: Tenant,
    *,
    feature: str,
    cron_name: str,
    prompt_template: str,
    fire_in_minutes: int = 5,
) -> WelcomeStatus:
    """Schedule a one-shot welcome cron for a tenant feature.

    ``feature`` is the key in ``Tenant.welcomes_sent`` (e.g. ``"fuel"``).
    ``cron_name`` is the gateway cron job name (e.g. ``"_fuel:welcome"``).
    ``prompt_template`` may contain ``{tenant_id}`` for interpolation.

    Raises any underlying gateway exception. Callers that want to
    swallow (live toggle path, async tasks) should wrap; the backfill
    and watchdog paths surface failures so telemetry is honest.
    """
    from apps.cron.gateway_client import _next_fire_at, cron_get, cron_remove, invoke_gateway_tool

    sent = (tenant.welcomes_sent or {}).get(feature)
    if sent:
        logger.info(
            "%s welcome already delivered for tenant %s at %s — skipping",
            feature,
            tenant.id,
            sent,
        )
        return WelcomeStatus.SKIPPED_ALREADY_DELIVERED

    existing = cron_get(tenant, cron_name)
    has_stale = False
    if existing is not None:
        # A welcome is "still pending" only if its next fire is within the
        # one-shot window. The encoding ``M H D M *`` is annual recurring,
        # so a welcome whose date already passed without successful
        # self-removal will report next_fire ~1 year in the future. That's
        # the canary 2026-05-07 incident: April 25 fire → agent crashed
        # mid-turn → cron stayed registered with next_fire = April 25, 2027.
        nxt = _next_fire_at(existing.get("schedule") or {})
        if nxt is not None and nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=UTC)
        if nxt is None or (nxt - datetime.now(nxt.tzinfo)) <= _ONE_SHOT_WINDOW:
            logger.info(
                "%s welcome already pending for tenant %s — skipping (idempotent)",
                feature,
                tenant.id,
            )
            return WelcomeStatus.SKIPPED_PENDING
        cron_remove(tenant, cron_name)
        has_stale = True
        logger.info(
            "%s welcome cron for tenant %s was stale (next fire %s) — removed before reschedule",
            feature,
            tenant.id,
            nxt.isoformat(),
        )

    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
    try:
        tz = zoneinfo.ZoneInfo(user_tz)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")
    fire_at = datetime.now(tz) + timedelta(minutes=fire_in_minutes)
    cron_expr = f"{fire_at.minute} {fire_at.hour} {fire_at.day} {fire_at.month} *"

    welcome_message = (
        prompt_template.format(tenant_id=tenant.id)
        + "\n\n---\n"
        + "After sending the welcome (and marking it delivered if the send succeeded), "
        + f"remove this cron: `cron remove {cron_name}`"
    )

    invoke_gateway_tool(
        tenant,
        "cron.add",
        {
            "job": {
                "name": cron_name,
                "schedule": {"kind": "cron", "expr": cron_expr, "tz": user_tz},
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": welcome_message},
                "delivery": {"mode": "none"},
                "enabled": True,
            }
        },
    )
    logger.info(
        "Scheduled %s welcome cron for tenant %s (fires at %s)",
        feature,
        tenant.id,
        fire_at.isoformat(),
    )
    return WelcomeStatus.REPLACED_STALE if has_stale else WelcomeStatus.SCHEDULED
