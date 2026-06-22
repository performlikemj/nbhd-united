"""Action expiry sweep — invoked via QStash cron, not Celery."""

from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def expire_stale_pending_actions() -> str:
    """Expire pending actions past their deadline and update platform messages.

    Run every 60 seconds via a QStash cron entry registered in TASK_MAP
    (see apps/cron/views.py).  Returns a summary string 'Expired N actions'.

    Errors in update_gate_message are caught and logged per action but do
    not abort the sweep, so one broken platform channel cannot stall expiry
    of other actions.
    """
    from .messaging import update_gate_message
    from .models import ActionAuditLog, ActionStatus, PendingAction

    stale = PendingAction.objects.filter(
        status=ActionStatus.PENDING,
        expires_at__lt=timezone.now(),
    )

    count = 0
    for action in stale:
        action.status = ActionStatus.EXPIRED
        action.save(update_fields=["status"])

        ActionAuditLog.objects.create(
            tenant=action.tenant,
            action_type=action.action_type,
            action_payload=action.action_payload,
            display_summary=action.display_summary,
            result=ActionStatus.EXPIRED,
        )

        try:
            update_gate_message(action)
        except Exception:
            logger.exception("Failed to update gate message for action %s", action.id)

        count += 1

    if count:
        logger.info("Expired %d stale pending actions", count)

    return f"Expired {count} actions"
