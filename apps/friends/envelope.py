"""USER.md Neighborhood envelope section + two-party refresh wiring.

Registered like :mod:`apps.lessons.envelope` via
:func:`apps.orchestrator.envelope_registry.register_section`, auto-wired from
``apps.friends.apps.FriendsConfig.ready()``, gated on ``friends_enabled``.

PR4 populates the section (accepted neighbor handles + newest un-purged absorbed
sparks) and fixes the two-party refresh gap flagged in PR0: the registry's
single-tenant ``_universal_refresh_receiver`` resolves the tenant from the
written row, which works for ``AbsorbedItem`` (its ``tenant`` FK is the absorber
who needs the refresh) but NOT for ``LessonShareGrant`` (the party who needs the
refresh is the friendship's OTHER side — the recipient — not any tenant on the
grant row). So we add an explicit grant receiver that refreshes the recipient.
Everything here is defensive: a refresh failure must never raise into a save().
"""

from __future__ import annotations

import logging
import threading

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_delete, post_save

from apps.orchestrator.envelope_registry import register_section
from apps.tenants.models import Tenant

from .models import AbsorbedItem, Friendship, LessonShareGrant, NeighborProfile

logger = logging.getLogger(__name__)

_MAX_HANDLES = 12
_MAX_SPARKS = 5


@register_section(
    key="neighborhood",
    heading="## Neighborhood — neighbors & sparks",
    enabled=lambda t: getattr(t, "friends_enabled", False),
    # AbsorbedItem's universal-receiver refresh resolves its ``tenant`` FK (the
    # absorber) correctly; Friendship no-ops (no single tenant); LessonShareGrant
    # no-ops in the universal receiver and is handled by the explicit receiver
    # below.
    refresh_on=(Friendship, LessonShareGrant, AbsorbedItem),
    order=63,
)
def render_neighborhood(tenant: Tenant) -> str:
    """TIGHT (≤~1KB): accepted neighbor handles + up to 5 newest un-purged
    absorbed sparks (title + @handle). Never raises."""
    try:
        edges = Friendship.objects.filter(
            Q(requester=tenant) | Q(addressee=tenant), status=Friendship.Status.ACCEPTED
        ).values_list("requester_id", "addressee_id")
        other_ids = [(r if a == tenant.id else a) for (r, a) in edges]
        handles = sorted(
            h for h in NeighborProfile.objects.filter(tenant_id__in=other_ids).values_list("handle", flat=True) if h
        )
        sparks = list(
            AbsorbedItem.objects.filter(
                tenant=tenant,
                purged_at__isnull=True,
                source_kind=AbsorbedItem.SourceKind.SHARED_LESSON,
            ).order_by("-absorbed_at")[:_MAX_SPARKS]
        )
        if not handles and not sparks:
            return ""

        lines: list[str] = []
        if handles:
            lines.append("Neighbors: " + ", ".join(f"@{h}" for h in handles[:_MAX_HANDLES]))
        if sparks:
            handle_by_id = dict(
                NeighborProfile.objects.filter(tenant_id__in=[s.from_tenant_id for s in sparks]).values_list(
                    "tenant_id", "handle"
                )
            )
            lines.append(
                "Sparks neighbors shared (hold until useful, then surface naturally; never claim you shared anything):"
            )
            for spark in sparks:
                who = handle_by_id.get(spark.from_tenant_id)
                title = (spark.label or "a shared spark").strip()[:100]
                lines.append(f"- {title}" + (f" — @{who}" if who else ""))
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — an envelope section must never break a turn
        logger.warning("render_neighborhood failed for tenant %s", getattr(tenant, "id", "?"), exc_info=True)
        return ""


# ── Explicit recipient refresh for LessonShareGrant (the two-party gap) ──────


def _schedule_recipient_push(tenant_id) -> None:
    if tenant_id is None:
        return

    def _push() -> None:
        from apps.orchestrator.workspace_envelope import push_user_md

        try:
            push_user_md(str(tenant_id), debounce_seconds=0)
        except Exception:
            logger.warning("friends recipient USER.md push failed for %s", str(tenant_id)[:8], exc_info=True)

    if getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False):
        transaction.on_commit(_push)
    else:
        transaction.on_commit(lambda: threading.Thread(target=_push, daemon=True).start())


def _refresh_recipient_on_grant(sender, instance, **kwargs) -> None:
    """Refresh the RECIPIENT's USER.md when a grant is created/revoked — the
    friendship's other party (the grant owner already sees their own share).
    Defensive: never raises."""
    try:
        friendship = instance.friendship
        if friendship is None:
            return  # circle grants (PR7) resolve recipients differently
        owner_id = instance.shared_lesson.owner_tenant_id
        recipient_id = friendship.addressee_id if friendship.requester_id == owner_id else friendship.requester_id
        _schedule_recipient_push(recipient_id)
    except Exception:  # noqa: BLE001
        logger.warning("grant recipient refresh receiver failed", exc_info=True)


# weak=False so the receiver lives for the process lifetime (mirrors the registry).
post_save.connect(_refresh_recipient_on_grant, sender=LessonShareGrant, weak=False)
post_delete.connect(_refresh_recipient_on_grant, sender=LessonShareGrant, weak=False)
