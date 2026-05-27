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

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from .models import CronCreationPath, CronJob

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
            idempotency_key=f"regen-cron-{tenant_id}",
            delay_seconds=30,
        )
    except Exception:
        logger.warning(
            "Failed to enqueue tenant cron regen for tenant %s",
            str(tenant_id)[:8],
            exc_info=True,
        )


@receiver(pre_save, sender=CronJob)
def cronjob_derive_data_from_typed_payload(sender, instance, **kwargs):
    """Regenerate ``CronJob.data`` from ``pattern + typed_payload`` on save.

    Invariant: for ``creation_path == TYPED`` rows, ``data`` is a derived
    view of the typed pattern's ``build_oc_data()`` output. Direct edits
    to ``data`` on a typed row are overwritten here. Freeform/legacy/
    internal rows are left alone — for those, ``data`` is the source of
    truth.

    Re-derives only when the pattern or typed_payload actually changed
    (compared against the existing DB row) so a no-op save doesn't churn
    the reconciler with an identical regenerated dict.
    """
    if instance.creation_path != CronCreationPath.TYPED:
        return
    if not instance.pattern:
        # CheckConstraint will reject this at the DB level, but defend
        # here so we don't crash in the handler lookup with a confusing
        # KeyError before the constraint fires.
        return

    if instance.pk:
        try:
            old = CronJob.objects.get(pk=instance.pk)
        except CronJob.DoesNotExist:
            old = None
        if (
            old is not None
            and old.pattern == instance.pattern
            and old.typed_payload == instance.typed_payload
            and old.name == instance.name
            and (old.data or {}).get("schedule") == (instance.data or {}).get("schedule")
        ):
            return

    # Local import: the patterns package imports Django models indirectly;
    # avoid a startup-time cycle by deferring.
    from apps.cron.patterns import get_handler

    handler = get_handler(instance.pattern)
    payload = handler.validate_payload(instance.typed_payload or {})
    schedule = (instance.data or {}).get("schedule")
    if not schedule:
        # The service layer is the canonical writer and always sets
        # data["schedule"] before save. If we get here a caller bypassed
        # the service; log loudly rather than silently writing a broken
        # OC dict.
        logger.error(
            "Typed CronJob save without data.schedule (tenant=%s name=%r pattern=%s) — skipping derive",
            str(instance.tenant_id)[:8],
            instance.name,
            instance.pattern,
        )
        return

    instance.data = handler.build_oc_data(
        payload,
        tenant=instance.tenant,
        name=instance.name,
        schedule=schedule,
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


# ---------------------------------------------------------------------------
# Test helpers — let the QuietCronSignalRunner mute the reconciler signal
# globally, and let signal-contract tests re-enable it per-class. Production
# code never calls these.


def disconnect_cronjob_reconcile_signals() -> None:
    """Disconnect both CronJob → reconciler signals (called by the test runner).

    Does NOT disconnect ``cronjob_derive_data_from_typed_payload``: that signal
    is a pure model-state derivation (no I/O, no QStash, no gateway calls)
    and tests that exercise the typed flow rely on it to populate ``data``.
    """
    post_save.disconnect(cronjob_saved_regen_tenant_crons, sender=CronJob)
    post_delete.disconnect(cronjob_deleted_regen_tenant_crons, sender=CronJob)


def connect_cronjob_reconcile_signals() -> None:
    """Reconnect both CronJob → reconciler signals (for tests that need them live)."""
    post_save.connect(cronjob_saved_regen_tenant_crons, sender=CronJob)
    post_delete.connect(cronjob_deleted_regen_tenant_crons, sender=CronJob)
