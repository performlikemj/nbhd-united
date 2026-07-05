"""USER.md Neighborhood envelope section.

Registered exactly like :mod:`apps.lessons.envelope` via
:func:`apps.orchestrator.envelope_registry.register_section`, auto-wired from
``apps.friends.apps.FriendsConfig.ready()``, gated on ``friends_enabled``.

PR0 is the invisible foundation: there is no cross-tenant content yet, so this
section renders the empty string. It MUST never raise — a raising envelope
section would break every agent turn for a flagged tenant. Later PRs populate
it (shared sparks / neighbors / recent chat highlights) and widen
``refresh_on`` as ``LessonShareGrant`` / ``FriendMessage`` / ``AbsorbedItem`` /
``CircleMembership`` land.
"""

from __future__ import annotations

from apps.orchestrator.envelope_registry import register_section
from apps.tenants.models import Tenant

from .models import Friendship


@register_section(
    key="neighborhood",
    heading="## Neighborhood — neighbors & sparks",
    enabled=lambda t: getattr(t, "friends_enabled", False),
    refresh_on=(Friendship,),
    order=63,
)
def render_neighborhood(tenant: Tenant) -> str:
    """Neighbors + newly-shared sparks. Empty until the PR2 share pipeline and
    PR5 chat land, so PR0 always returns ``""``."""
    return ""
