"""Celery tasks for action gating."""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def expire_stale_pending_actions():
    """Expire pending actions past their deadline.

    Run every 60 seconds via Celery Beat (or QStash cron).
    """
    from .models import ActionAuditLog, ActionStatus, PendingAction
    from .messaging import update_gate_message

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
