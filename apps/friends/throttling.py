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
