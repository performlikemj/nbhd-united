"""Wire model post_save / post_delete signals to cache-tag bumps.

Each mutation invalidates the smallest set of tags that could be affected.
Keep this module the only place that calls `bump_tag` for model events, so
the read-side decorator only needs to know the tag name.
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .cache import bump_tag, bump_tags

logger = logging.getLogger("nbhd.cache.signals")


def _tenant_id(instance):
    tenant = getattr(instance, "tenant", None)
    if tenant is not None:
        return getattr(tenant, "id", tenant)
    return getattr(instance, "tenant_id", None)


def _bump(instance, tags):
    tenant_id = _tenant_id(instance)
    if not tenant_id:
        return
    try:
        bump_tags(tenant_id, tags)
    except Exception:
        logger.exception("bump_tags failed for %s tags=%s", instance, tags)


def _register():
    from apps.fuel.models import (
        BodyWeightLog,
        FuelGoal,
        FuelProfile,
        PersonalRecord,
        RestingHeartRateLog,
        SleepLog,
        Workout,
        WorkoutPlan,
    )
    from apps.journal.models import DailyNote, Document, JournalEntry
    from apps.tenants.models import Tenant

    @receiver(post_save, sender=Workout)
    @receiver(post_delete, sender=Workout)
    def _workout_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel", "dashboard"])

    @receiver(post_save, sender=WorkoutPlan)
    @receiver(post_delete, sender=WorkoutPlan)
    def _plan_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel"])

    @receiver(post_save, sender=BodyWeightLog)
    @receiver(post_delete, sender=BodyWeightLog)
    def _bodyweight_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel", "dashboard"])

    @receiver(post_save, sender=PersonalRecord)
    @receiver(post_delete, sender=PersonalRecord)
    def _pr_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel"])

    @receiver(post_save, sender=RestingHeartRateLog)
    @receiver(post_delete, sender=RestingHeartRateLog)
    def _rhr_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel"])

    @receiver(post_save, sender=SleepLog)
    @receiver(post_delete, sender=SleepLog)
    def _sleep_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel"])

    @receiver(post_save, sender=FuelGoal)
    @receiver(post_delete, sender=FuelGoal)
    def _goal_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel"])

    @receiver(post_save, sender=FuelProfile)
    @receiver(post_delete, sender=FuelProfile)
    def _profile_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel"])

    @receiver(post_save, sender=JournalEntry)
    @receiver(post_delete, sender=JournalEntry)
    def _journal_entry_changed(sender, instance, **kwargs):
        _bump(instance, ["journal", "dashboard"])

    @receiver(post_save, sender=Document)
    @receiver(post_delete, sender=Document)
    def _document_changed(sender, instance, **kwargs):
        _bump(instance, ["journal", "sidebar"])

    @receiver(post_save, sender=DailyNote)
    @receiver(post_delete, sender=DailyNote)
    def _daily_note_changed(sender, instance, **kwargs):
        _bump(instance, ["journal", "dashboard"])

    @receiver(post_save, sender=Tenant)
    def _tenant_changed(sender, instance, **kwargs):
        try:
            bump_tag(instance.id, "dashboard")
            bump_tag(instance.id, "tenant")
        except Exception:
            logger.exception("tenant bump failed for %s", instance.id)


_register()
