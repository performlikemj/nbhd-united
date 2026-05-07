"""Signals — Workout writes trigger debounced Fuel cron regeneration.

Source-of-truth inversion: when a tenant has opted into the new session-based
scheduling (`FuelProfile.use_session_scheduling=True`), every Workout save or
delete enqueues a 30s-debounced QStash task that diffs the desired cron set
(derived from `Workout.scheduled_at`) against what's in the OpenClaw container
and applies the minimal change.

The 30s debounce + idempotency_key collapse rapid sequential edits (e.g.
drag-to-reschedule that fires onDrop, then immediately a server PATCH) into
one regeneration call.
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Workout

logger = logging.getLogger(__name__)


def _tenant_uses_session_scheduling(workout: Workout) -> bool:
    """True when this Workout's tenant has opted into the new flow."""
    profile = getattr(workout.tenant, "fuel_profile", None)
    return bool(profile and profile.use_session_scheduling)


def _enqueue_regen(tenant_id: str) -> None:
    """Enqueue a debounced regen task; swallow errors so a save still succeeds."""
    from apps.cron.publish import publish_task

    try:
        publish_task(
            "regenerate_fuel_crons",
            tenant_id,
            idempotency_key=f"regen-fuel:{tenant_id}",
            delay_seconds=30,
        )
    except Exception:
        logger.warning(
            "Failed to enqueue Fuel cron regen for tenant %s",
            str(tenant_id)[:8],
            exc_info=True,
        )


@receiver(post_save, sender=Workout)
def workout_saved_regen_fuel_crons(sender, instance, **kwargs):
    if not _tenant_uses_session_scheduling(instance):
        return
    _enqueue_regen(str(instance.tenant_id))


@receiver(post_delete, sender=Workout)
def workout_deleted_regen_fuel_crons(sender, instance, **kwargs):
    if not _tenant_uses_session_scheduling(instance):
        return
    _enqueue_regen(str(instance.tenant_id))


# USER.md refresh on Fuel state changes is auto-wired by the envelope
# registry (apps/fuel/envelope.py registers Workout / BodyWeightLog /
# SleepLog as ``refresh_on`` triggers). Don't add USER.md push handlers
# here — that path is owned by the registry.
