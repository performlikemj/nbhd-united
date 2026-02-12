"""Scheduler utilities for processing due automations."""
from __future__ import annotations

import logging
from datetime import datetime

from django.utils import timezone

from .models import Automation, AutomationRun
from .services import execute_automation

logger = logging.getLogger(__name__)

DEFAULT_DUE_BATCH_SIZE = 100


def get_due_automations(*, now: datetime | None = None, limit: int = DEFAULT_DUE_BATCH_SIZE):
    reference = now or timezone.now()
    return (
        Automation.objects.select_related("tenant", "tenant__user")
        .filter(
            status=Automation.Status.ACTIVE,
            next_run_at__lte=reference,
        )
        .order_by("next_run_at")[:limit]
    )


def run_due_automations(*, now: datetime | None = None, limit: int = DEFAULT_DUE_BATCH_SIZE) -> dict:
    reference = now or timezone.now()
    due = list(get_due_automations(now=reference, limit=limit))

    summary = {
        "due_count": len(due),
        "processed_count": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
    }

    for automation in due:
        try:
            run = execute_automation(
                automation=automation,
                trigger_source=AutomationRun.TriggerSource.SCHEDULE,
                scheduled_for=automation.next_run_at,
            )
        except Exception:
            summary["errors"] += 1
            logger.exception("Failed scheduled automation run for automation=%s", automation.id)
            continue

        summary["processed_count"] += 1
        if run.status == AutomationRun.Status.SUCCEEDED:
            summary["succeeded"] += 1
        elif run.status == AutomationRun.Status.FAILED:
            summary["failed"] += 1
        elif run.status == AutomationRun.Status.SKIPPED:
            summary["skipped"] += 1

    return summary
