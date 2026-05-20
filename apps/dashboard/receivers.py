"""Cache-invalidation receivers for the dashboard tag.

The dashboard cache (see ``apps/common/cache.py``) is keyed by
``(view qualname, tenant id, tag version, request signature)`` and
invalidated by bumping the tag version. This module wires every model
the dashboard reads to a ``bump_tag(tenant_id, "dashboard")`` call so
that mutations take effect immediately rather than waiting up to the
60-second TTL.

Why this matters: the frontend's optimistic UI for insight confirm /
refute mutations (see ``useApproveInsightMutation`` /
``useDismissInsightMutation``) invalidates the ``["horizons"]`` React
Query key on click and refetches. Without backend invalidation, the
refetch hits the still-cached response and reverts the optimistic
flip — visibly worse than no optimism at all.

Models covered:
- ``AssistantInsight`` — confirm/refute via Horizons buttons; also drives
  Phase 3 Day 2's ``topic_signals`` confirmed/refuted counts.
- ``UserVoicePref`` — ``register_offset`` flips via chat tool call;
  ``topic_signals`` reflects the override.
- ``Goal`` — typed-Goal CRUD (post-#624); HorizonsView "Active goals"
  list + ``intent.has_stated_goal`` signal.
- ``Document`` — legacy goals/tasks docs still read by HorizonsView
  for non-migrated tenants.

``Task`` is intentionally excluded — HorizonsView doesn't surface Task
state, and ``compute_signals`` doesn't read Task. Add a receiver here
if that changes.
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.common.cache import bump_tag
from apps.insights.models import AssistantInsight, UserVoicePref
from apps.journal.models import Document, Goal

logger = logging.getLogger("nbhd.cache")

_TAG = "dashboard"


def _bump_for(instance) -> None:
    """Bump the dashboard tag for ``instance.tenant``.

    Swallows any exception so a cache backend hiccup never bubbles up
    into the model save and breaks the request. ``bump_tag`` itself
    already logs and returns 0 on Redis failures, but we double-belt
    here because receivers fire on every write.
    """
    tenant_id = getattr(instance, "tenant_id", None)
    if tenant_id is None:
        return
    try:
        bump_tag(tenant_id, _TAG)
    except Exception:
        logger.exception("dashboard cache bump failed for tenant %s", tenant_id)


@receiver(post_save, sender=AssistantInsight)
@receiver(post_delete, sender=AssistantInsight)
def _bump_on_assistant_insight(sender, instance, **kwargs):
    _bump_for(instance)


@receiver(post_save, sender=UserVoicePref)
@receiver(post_delete, sender=UserVoicePref)
def _bump_on_user_voice_pref(sender, instance, **kwargs):
    _bump_for(instance)


@receiver(post_save, sender=Goal)
@receiver(post_delete, sender=Goal)
def _bump_on_goal(sender, instance, **kwargs):
    _bump_for(instance)


@receiver(post_save, sender=Document)
@receiver(post_delete, sender=Document)
def _bump_on_document(sender, instance, **kwargs):
    # Any Document kind matters — the dashboard surfaces goals, tasks,
    # weekly reviews (Weekly Pulse), and daily notes (momentum streak).
    _bump_for(instance)
