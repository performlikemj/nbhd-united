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
import threading
from contextlib import contextmanager

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Workout, WorkoutPlan

logger = logging.getLogger(__name__)

_SUPPRESS_REGEN = threading.local()


@contextmanager
def suppress_cron_regen():
    """Silence the per-row Fuel cron-regen enqueue for writes in the block.

    Each enqueue is a blocking QStash HTTPS publish (and runs the task
    inline-synchronously in dev without a QStash token) — a 50-row
    HealthKit batch would make 50 of them. The batch caller owns issuing
    one ``_enqueue_regen`` after the loop.
    """
    prev = getattr(_SUPPRESS_REGEN, "active", False)
    _SUPPRESS_REGEN.active = True
    try:
        yield
    finally:
        _SUPPRESS_REGEN.active = prev


def _bump_fuel_version(tenant_id) -> None:
    """Increment tenant.fuel_version atomically. Best-effort; logged on fail.

    Phase 6 — surfaces an out-of-band write in schedule / calendar
    responses so the frontend can prompt the user to refresh a stale
    drawer before they save. Cheap enough to run on every Fuel write.
    """
    if tenant_id is None:
        return
    try:
        from django.db.models import F

        from apps.tenants.models import Tenant

        Tenant.objects.filter(id=tenant_id).update(fuel_version=F("fuel_version") + 1)
    except Exception:
        logger.warning("Failed to bump tenant.fuel_version for %s", str(tenant_id)[:8], exc_info=True)


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
            idempotency_key=f"regen-fuel-{tenant_id}",
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
    if getattr(_SUPPRESS_REGEN, "active", False):
        return
    if not _tenant_uses_session_scheduling(instance):
        return
    _enqueue_regen(str(instance.tenant_id))


@receiver(post_delete, sender=Workout)
def workout_deleted_regen_fuel_crons(sender, instance, **kwargs):
    if getattr(_SUPPRESS_REGEN, "active", False):
        return
    if not _tenant_uses_session_scheduling(instance):
        return
    _enqueue_regen(str(instance.tenant_id))


@receiver(post_save, sender=Workout)
def workout_saved_bump_fuel_version(sender, instance, **kwargs):
    _bump_fuel_version(instance.tenant_id)


@receiver(post_delete, sender=Workout)
def workout_deleted_bump_fuel_version(sender, instance, **kwargs):
    _bump_fuel_version(instance.tenant_id)


@receiver(post_save, sender=WorkoutPlan)
def plan_saved_bump_fuel_version(sender, instance, **kwargs):
    _bump_fuel_version(instance.tenant_id)


@receiver(post_delete, sender=WorkoutPlan)
def plan_deleted_bump_fuel_version(sender, instance, **kwargs):
    _bump_fuel_version(instance.tenant_id)


_TOMBSTONE_CAP = 200


@receiver(post_delete, sender=Workout)
def workout_deleted_record_healthkit_tombstone(sender, instance, **kwargs):
    """Remember deleted HK-anchored rows so a sync anchor reset (app
    reinstall, 30-day re-backfill) cannot resurrect them — the same
    failure class as the task-resurrection incident (PR #847).

    Gated on external_id alone, NOT source: a matched planned session
    keeps its user/assistant source but carries the HK sample UUID, and
    deleting it must also block re-import.

    Only records when a FuelProfile already exists (never creates one —
    cascade tenant deletes pass through here too).
    """
    if not instance.external_id:
        return
    try:
        from .models import FuelProfile

        profile = FuelProfile.objects.filter(tenant_id=instance.tenant_id).first()
        if profile is None:
            return
        stones = list(profile.healthkit_tombstones or [])
        if instance.external_id in stones:
            return
        stones.append(instance.external_id)
        profile.healthkit_tombstones = stones[-_TOMBSTONE_CAP:]
        profile.save(update_fields=["healthkit_tombstones", "updated_at"])
    except Exception:
        logger.warning(
            "Failed to record HealthKit tombstone for tenant %s",
            str(instance.tenant_id)[:8],
            exc_info=True,
        )


# USER.md refresh on Fuel state changes is auto-wired by the envelope
# registry (apps/fuel/envelope.py registers Workout / BodyWeightLog /
# SleepLog / RestingHeartRateLog as ``refresh_on`` triggers). Don't add
# USER.md push handlers here — that path is owned by the registry.
