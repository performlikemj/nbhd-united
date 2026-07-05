"""DRF throttles for the Neighborhood console (JWT-authed UI path).

Mirrors ``apps.tenants.throttling`` — a per-user ``SimpleRateThrottle`` whose
``rate`` is set on the class (no ``DEFAULT_THROTTLE_RATES`` needed).
"""

from __future__ import annotations

from rest_framework.throttling import SimpleRateThrottle


class _UserScopedThrottle(SimpleRateThrottle):
    """Throttle a JWT-authed request keyed by user id. Returns None (unthrottled)
    for unauthenticated requests — the view's IsAuthenticated handles those."""

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": str(request.user.pk)}


class WaveSendDayThrottle(_UserScopedThrottle):
    """A wave is a low-stakes social knock; a person sends a handful a day.
    This caps wave-spam / invite-bombing to a sane daily budget."""

    scope = "friend_wave_day"
    rate = "10/day"


class ShareSendDayThrottle(_UserScopedThrottle):
    """Sharing a lesson kicks off a fail-closed DeBERTa scrub (real compute).
    Generous for a normal day of sharing, but caps a runaway loop / abuse."""

    scope = "friend_share_day"
    rate = "30/day"


class AdoptDayThrottle(_UserScopedThrottle):
    """Adopting a neighbor's spark ("bring it home") mints a pending lesson.
    Generous daily budget; the cap only bites automated hammering."""

    scope = "friend_adopt_day"
    rate = "60/day"


class MessageSendHourThrottle(_UserScopedThrottle):
    """Friend/circle chat send. Hourly (not daily) so a lively conversation is
    never blocked, while a flood/abuse burst is capped."""

    scope = "friend_message_hour"
    rate = "60/hour"
