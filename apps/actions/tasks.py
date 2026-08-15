"""Action expiry sweep — invoked via QStash cron, not Celery."""

from __future__ import annotations

import logging

from django.db import transaction
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
    from apps.datebook.gate import (
        expire_datebook_action,
        is_datebook_action_type,
    )

    from .messaging import update_gate_message
    from .models import ActionStatus, PendingAction
    from .services import record_action_audit

    stale = PendingAction.objects.select_related("tenant__user").filter(
        status=ActionStatus.PENDING,
        expires_at__lt=timezone.now(),
    )

    count = 0
    for action in stale:
        if is_datebook_action_type(action.action_type):
            with transaction.atomic():
                locked = PendingAction.objects.select_for_update().select_related("tenant__user").get(pk=action.pk)
                if locked.status != ActionStatus.PENDING or locked.expires_at >= timezone.now():
                    continue
                expire_datebook_action(locked)
                action = locked
        else:
            # Conditional update: only flip PENDING→EXPIRED; skip if another writer
            # (e.g. a concurrent Approve) has already resolved the row.
            updated = PendingAction.objects.filter(
                id=action.id,
                status=ActionStatus.PENDING,
            ).update(status=ActionStatus.EXPIRED)
            if not updated:
                continue
            action.status = ActionStatus.EXPIRED

            record_action_audit(action, ActionStatus.EXPIRED)

        try:
            update_gate_message(action)
        except Exception:
            logger.exception("Failed to update gate message for action %s", action.id)

        count += 1

    if count:
        logger.info("Expired %d stale pending actions", count)

    return f"Expired {count} actions"
