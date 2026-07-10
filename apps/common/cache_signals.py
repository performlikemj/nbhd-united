"""Wire model post_save / post_delete signals to cache-tag bumps.

Each mutation invalidates the smallest set of tags that could be affected.
Keep this module the only place that calls `bump_tag` for model events, so
the read-side decorator only needs to know the tag name.

IMPORTANT — every ``@receiver(...)`` below MUST pass ``weak=False``. These
receivers are LOCAL functions defined inside ``_register()``, and Django's
``@receiver`` decorator defaults to ``weak=True``. Once ``_register()``
returns, a weakly-connected local closure has no remaining strong reference
anywhere, so it is garbage collected immediately and silently stops firing —
`bump_tag`/`bump_tags` never run again, and every `tenant_cache`-decorated
endpoint then serves stale data for its full TTL after any write. This is
invisible in local/test runs (``settings.DEBUG=True`` makes
``Signal.connect()`` run a validity check that incidentally keeps the
receiver alive via an unrelated ``functools.lru_cache``), but real in every
production process (``DEBUG=False`` skips that path entirely). See
``apps.common.tests.CacheSignalReceiverLivenessTest`` for the regression test.
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
    from apps.journal.models import DailyNote, Document, Goal, JournalEntry, Task
    from apps.tenants.models import Tenant

    @receiver(post_save, sender=Workout, weak=False)
    @receiver(post_delete, sender=Workout, weak=False)
    def _workout_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel", "dashboard"])

    @receiver(post_save, sender=WorkoutPlan, weak=False)
    @receiver(post_delete, sender=WorkoutPlan, weak=False)
    def _plan_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel"])

    @receiver(post_save, sender=BodyWeightLog, weak=False)
    @receiver(post_delete, sender=BodyWeightLog, weak=False)
    def _bodyweight_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel", "dashboard"])

    @receiver(post_save, sender=PersonalRecord, weak=False)
    @receiver(post_delete, sender=PersonalRecord, weak=False)
    def _pr_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel"])

    @receiver(post_save, sender=RestingHeartRateLog, weak=False)
    @receiver(post_delete, sender=RestingHeartRateLog, weak=False)
    def _rhr_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel"])

    @receiver(post_save, sender=SleepLog, weak=False)
    @receiver(post_delete, sender=SleepLog, weak=False)
    def _sleep_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel"])

    @receiver(post_save, sender=FuelGoal, weak=False)
    @receiver(post_delete, sender=FuelGoal, weak=False)
    def _goal_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel"])

    @receiver(post_save, sender=FuelProfile, weak=False)
    @receiver(post_delete, sender=FuelProfile, weak=False)
    def _profile_changed(sender, instance, **kwargs):
        _bump(instance, ["fuel"])

    @receiver(post_save, sender=JournalEntry, weak=False)
    @receiver(post_delete, sender=JournalEntry, weak=False)
    def _journal_entry_changed(sender, instance, **kwargs):
        _bump(instance, ["journal", "dashboard"])

    @receiver(post_save, sender=Document, weak=False)
    @receiver(post_delete, sender=Document, weak=False)
    def _document_changed(sender, instance, **kwargs):
        _bump(instance, ["journal", "sidebar"])

    @receiver(post_save, sender=DailyNote, weak=False)
    @receiver(post_delete, sender=DailyNote, weak=False)
    def _daily_note_changed(sender, instance, **kwargs):
        _bump(instance, ["journal", "dashboard"])

    @receiver(post_save, sender=Task, weak=False)
    @receiver(post_delete, sender=Task, weak=False)
    def _task_changed(sender, instance, **kwargs):
        _bump(instance, ["journal", "dashboard"])

    @receiver(post_save, sender=Goal, weak=False)
    @receiver(post_delete, sender=Goal, weak=False)
    def _typed_goal_changed(sender, instance, **kwargs):
        _bump(instance, ["journal", "dashboard"])

    @receiver(post_save, sender=Tenant, weak=False)
    def _tenant_changed(sender, instance, **kwargs):
        try:
            bump_tag(instance.id, "dashboard")
            bump_tag(instance.id, "tenant")
        except Exception:
            logger.exception("tenant bump failed for %s", instance.id)


_register()
