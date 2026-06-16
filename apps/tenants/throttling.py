"""DRF throttle classes for PAT-authed traffic and PAT minting.

External callers (YardTalk, future skills) push to NU under a PAT.
Throttle by PAT id so a single noisy app cannot exhaust a user's budget
across all their tokens, and so revoking one bad token isolates blast
radius.
"""

from __future__ import annotations

from rest_framework.throttling import SimpleRateThrottle


class _PATScopedThrottle(SimpleRateThrottle):
    """Throttle a PAT-authed request keyed by PAT id.

    Returns None for non-PAT auth so JWT/UI traffic is not throttled here.
    """

    def get_cache_key(self, request, view):
        pat = getattr(request, "auth_pat", None)
        if pat is None:
            return None
        return self.cache_format % {"scope": self.scope, "ident": str(pat.id)}


class PATSessionIngestMinuteThrottle(_PATScopedThrottle):
    scope = "pat_session_minute"
    rate = "60/minute"


class PATSessionIngestDayThrottle(_PATScopedThrottle):
    scope = "pat_session_day"
    rate = "5000/day"


class UserPATMintHourThrottle(SimpleRateThrottle):
    """Throttle PAT minting per user (JWT-authed UI path)."""

    scope = "user_pat_mint_hour"
    rate = "10/hour"

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": str(request.user.pk)}


class _UserScopedThrottle(SimpleRateThrottle):
    """Throttle a JWT-authed request keyed by user id."""

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": str(request.user.pk)}


class ChatLocalTurnHourThrottle(_UserScopedThrottle):
    """On-device turn records are human-paced (one per chat exchange); this
    only has to stop a runaway client from minting unbounded rows on a
    budget-exempt endpoint."""

    scope = "chat_local_turn_hour"
    rate = "240/hour"


class ChatContextHourThrottle(_UserScopedThrottle):
    """The context digest renders every envelope section per call; clients
    cache it for 15 minutes, so even multi-device use stays tiny."""

    scope = "chat_context_hour"
    rate = "120/hour"


class SiriRespondMinuteThrottle(_UserScopedThrottle):
    """The Tier-2 fast responder calls a model on every request (platform ZDR
    key, platform-absorbed cost). Human voice pacing is a few per minute; this
    only stops a runaway client from racking up model spend or hammering
    OpenRouter."""

    scope = "siri_respond_minute"
    rate = "30/minute"


class SiriStatusMinuteThrottle(_UserScopedThrottle):
    """The status snapshot is a deterministic no-LLM read, but still bound it
    so a stuck client can't poll it in a tight loop."""

    scope = "siri_status_minute"
    rate = "60/minute"
