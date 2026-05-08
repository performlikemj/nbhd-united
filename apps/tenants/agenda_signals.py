"""Signal handlers that mirror existing tenant state into AgendaEngagement.

Phase B introduces ``AgendaEngagement`` as the engagement-metadata
overlay. Several existing flows already produce engagement-shaped
signals — when a welcome cron successfully delivers, the platform
sets ``Tenant.welcomes_sent[feature]``. That's a real "thread X
transitioned" event; mirroring it into ``AgendaEngagement`` lets the
renderer apply consistent suppression rules without each call site
having to know about engagement bookkeeping.

The mirroring is one-way (tenant → engagement) and idempotent — a
welcomes_sent flip from null to a timestamp marks the corresponding
``feature_intro`` engagement row as ``COMPLETED`` with
``last_surfaced_at`` set to that timestamp. Subsequent saves don't
re-mutate (the row's already in the right state).

Wired from ``apps.tenants.AppConfig.ready()``.
"""

from __future__ import annotations

import logging
from datetime import datetime

from django.db.models.signals import post_save
from django.dispatch import receiver

from .agenda_models import AgendaEngagement
from .agenda_service import mark_state, mark_surfaced
from .models import Tenant

logger = logging.getLogger(__name__)


# Snapshot ``welcomes_sent`` immediately before save so the post_save
# handler can diff against it. We can't read from the DB inside
# post_save (the row's already updated), and pre_save fires before the
# instance has its post-save id, so we cache here.
_PRE_SAVE_WELCOMES: dict[str, dict] = {}


def _snapshot_welcomes_sent(sender, instance: Tenant, **_kwargs) -> None:
    """pre_save: capture the prior ``welcomes_sent`` for diffing later."""
    if not instance.pk:
        # New tenant — nothing prior; treat all keys as "newly set"
        _PRE_SAVE_WELCOMES[str(instance.pk or "")] = {}
        return
    try:
        prior = Tenant.objects.values_list("welcomes_sent", flat=True).get(pk=instance.pk)
    except Tenant.DoesNotExist:
        prior = {}
    _PRE_SAVE_WELCOMES[str(instance.pk)] = dict(prior or {})


@receiver(post_save, sender=Tenant)
def _mirror_welcomes_to_engagement(sender, instance: Tenant, created: bool, **_kwargs) -> None:
    """post_save: when a welcomes_sent key flips null → timestamp,
    mirror that into the corresponding AgendaEngagement row.

    Only the *transition* counts. A welcomes_sent value that's been
    set for a while doesn't trigger anything on every Tenant.save;
    we diff against the pre-save snapshot.

    Mirror writes are deferred to ``transaction.on_commit`` so they
    happen *after* the outer Tenant.save transaction commits — avoids
    nested savepoint accumulation that contributed to the
    2026-05-08 test-database teardown flake. In ``TestCase`` tests
    (transactional, rolled back), on_commit doesn't fire — the mirror
    is best-effort, and tests that exercise it use TransactionTestCase.
    """
    from django.db import transaction

    pk = str(instance.pk)
    prior = _PRE_SAVE_WELCOMES.pop(pk, {})
    current = dict(instance.welcomes_sent or {})

    transitions: list[tuple[str, str]] = []
    for feature, ts_raw in current.items():
        if not ts_raw:
            continue
        if prior.get(feature):
            # Was already set — not a fresh transition.
            continue
        transitions.append((feature, str(ts_raw)))

    if not transitions:
        return

    instance_id = str(instance.id)[:8]

    def _apply_mirror() -> None:
        from django.utils import timezone

        for feature, ts_raw in transitions:
            try:
                when = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                when = timezone.now()

            try:
                mark_surfaced(
                    instance,
                    kind=AgendaEngagement.Kind.FEATURE_INTRO,
                    item_id=feature,
                    when=when,
                    signal="welcome_delivered",
                )
                mark_state(
                    instance,
                    kind=AgendaEngagement.Kind.FEATURE_INTRO,
                    item_id=feature,
                    state=AgendaEngagement.State.COMPLETED,
                )
            except Exception:
                logger.exception(
                    "agenda_signals: failed to mirror welcomes_sent[%s] for tenant %s",
                    feature,
                    instance_id,
                )

    transaction.on_commit(_apply_mirror)


def connect_signals() -> None:
    """Wire the pre_save snapshot. ``post_save`` is wired via the
    ``@receiver`` decorator so it activates on import."""
    from django.db.models.signals import pre_save

    pre_save.connect(_snapshot_welcomes_sent, sender=Tenant, weak=False)
