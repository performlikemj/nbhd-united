"""Signals — CronJob writes trigger debounced reconcile to OpenClaw SQLite.

Postgres CronJob rows are the canonical source of truth for tenants on the
``postgres_cron_canonical`` flow. Every save/delete enqueues a 30s-debounced
QStash task that diffs Postgres against the container's current SQLite state
and applies the minimal change.

The debounce + idempotency_key collapse rapid sequential edits (drag-to-
reschedule, bulk-delete, etc.) into a single reconcile call.

This is the broad-surface generalization of the per-Workout signal pattern
from ``apps/fuel/signals.py``.
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import CronJob

logger = logging.getLogger(__name__)


def _tenant_uses_postgres_canonical(cronjob: CronJob) -> bool:
    """True when this CronJob's tenant has opted into the new flow."""
    tenant = getattr(cronjob, "tenant", None)
    return bool(tenant and getattr(tenant, "postgres_cron_canonical", False))


def _enqueue_regen(tenant_id: str) -> None:
    """Enqueue a debounced reconcile task; swallow errors so a save still succeeds."""
    from apps.cron.publish import publish_task

    try:
        publish_task(
            "regenerate_tenant_crons",
            tenant_id,
            idempotency_key=f"regen-cron:{tenant_id}",
            delay_seconds=30,
        )
    except Exception:
        logger.warning(
            "Failed to enqueue tenant cron regen for tenant %s",
            str(tenant_id)[:8],
            exc_info=True,
        )


@receiver(post_save, sender=CronJob)
def cronjob_saved_regen_tenant_crons(sender, instance, **kwargs):
    if not _tenant_uses_postgres_canonical(instance):
        return
    _enqueue_regen(str(instance.tenant_id))


@receiver(post_delete, sender=CronJob)
def cronjob_deleted_regen_tenant_crons(sender, instance, **kwargs):
    if not _tenant_uses_postgres_canonical(instance):
        return
    _enqueue_regen(str(instance.tenant_id))
