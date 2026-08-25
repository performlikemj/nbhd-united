"""Action expiry sweep — invoked via QStash cron, not Celery."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

CRON_DISPATCH_REPLAY_DELAY = timedelta(minutes=5)
CRON_DISPATCH_REPLAY_BATCH_SIZE = 50


def expire_stale_pending_actions() -> str:
    """Expire pending actions past their deadline and update platform messages.

    Run every five minutes via a QStash cron entry registered in TASK_MAP
    (see apps/cron/views.py).  Returns a summary string 'Expired N actions'.

    Errors in update_gate_message are caught and logged per action but do
    not abort the sweep, so one broken platform channel cannot stall expiry
    of other actions.
    """
    from apps.cron.gate import expire_cron_action, is_cron_action_type
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
        if is_datebook_action_type(action.action_type) or is_cron_action_type(action.action_type):
            with transaction.atomic():
                locked = PendingAction.objects.select_for_update().select_related("tenant__user").get(pk=action.pk)
                if locked.status != ActionStatus.PENDING or locked.expires_at >= timezone.now():
                    continue
                if is_datebook_action_type(locked.action_type):
                    expire_datebook_action(locked)
                else:
                    expire_cron_action(locked)
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


def replay_stale_cron_dispatches() -> str:
    """Replay committed cron outbox rows whose original callback was lost."""

    from apps.cron.gate import CRON_DISPATCH_LEASE, dispatch_cron_action
    from apps.datebook.notify import dispatch_datebook_gate_changed

    from .messaging import update_gate_message
    from .models import (
        ActionAuditLog,
        ActionAuditOutcome,
        ActionType,
        CronDispatch,
        CronDispatchState,
    )

    now = timezone.now()
    terminal_audits = ActionAuditLog.objects.filter(
        tenant_id=OuterRef("action__tenant_id"),
        action_type=ActionType.CRON_CREATE,
        responded_at=OuterRef("action__responded_at"),
        result__in=(ActionAuditOutcome.EXECUTED, ActionAuditOutcome.FAILED),
    )
    due = (
        CronDispatch.objects.annotate(has_terminal_audit=Exists(terminal_audits))
        .filter(has_terminal_audit=False)
        .filter(
            Q(
                state=CronDispatchState.QUEUED,
                created_at__lte=now - CRON_DISPATCH_REPLAY_DELAY,
            )
            | Q(
                state=CronDispatchState.DISPATCHING,
            )
            & (Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lte=now - CRON_DISPATCH_LEASE))
        )
        .order_by("created_at", "id")
        .values_list("id", flat=True)[:CRON_DISPATCH_REPLAY_BATCH_SIZE]
    )

    counts = {"executed": 0, "failed": 0, "queued": 0, "busy": 0}
    for dispatch_id in list(due):
        state = dispatch_cron_action(dispatch_id)
        counts[state] = counts.get(state, 0) + 1
        if state not in (CronDispatchState.EXECUTED, CronDispatchState.FAILED):
            continue
        dispatch = CronDispatch.objects.select_related("action__tenant").get(pk=dispatch_id)
        try:
            update_gate_message(dispatch.action)
        except Exception:
            logger.exception("Failed to update replayed cron gate message for dispatch %s", dispatch_id)
        dispatch_datebook_gate_changed(dispatch.action.tenant_id)

    total = sum(counts.values())
    if total:
        logger.info("Replayed %d cron dispatches: %s", total, counts)
    return (
        f"Cron dispatch replayed {total}: executed={counts['executed']} "
        f"failed={counts['failed']} queued={counts['queued']} busy={counts['busy']}"
    )
