"""LINE Messaging-API Push-message quota client + state machine.

The platform shares a single LINE channel across all tenants. The
Messaging-API plan caps how many Push messages we can send per calendar
month — once exhausted, every Push call returns 429 with
``{"message":"You have reached your monthly limit."}``. That sinks
proactive crons (which always Push) and any inbound reply whose
``reply_token`` has expired by the time the agent responds.

This module is the single source of truth for "is LINE Push currently
usable, and how much headroom is left." Two inputs feed the state:

  1. A daily poll of ``/v2/bot/message/quota`` (limit) +
     ``/v2/bot/message/quota/consumption`` (used) — see
     ``refresh_quota_state``.
  2. A live tripwire on the Push send paths — see
     ``mark_quota_exhausted_from_429``. Bridges the gap between two
     daily polls if the cap gets crossed mid-day.

Both feed the singleton :class:`LineQuotaState`. Anything else that
needs to know about quota state (frontend gate, transition handlers,
built-in heartbeat gate) reads from that row.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, NamedTuple

import httpx
from django.conf import settings
from django.utils import timezone

if TYPE_CHECKING:
    from apps.tenants.models import User

logger = logging.getLogger(__name__)


# Threshold for the pre-warn email (operational signal to the platform
# owner that we're about to hit the cap). Fires once per ratio-crossing;
# cleared when usage drops back below.
PRE_WARN_THRESHOLD = 0.90

# JSON key in ``User.preferences`` that records "we flipped this user
# off LINE because the platform quota was exhausted." Used so the
# recovery handler knows who to email when LINE comes back.
USER_PREF_FLIPPED_BY_QUOTA = "channel_flipped_by_quota"

# LINE API base.
_LINE_API_BASE = "https://api.line.me/v2/bot/message"

# Substring that identifies a 429 as the monthly-cap exhaustion rather
# than a generic rate-limit. LINE returns this in the response body.
_MONTHLY_LIMIT_SIGNAL = "monthly limit"


# ─────────────────────────────────────────────────────────────────────
# State transitions returned by refresh_quota_state — drives the
# downstream handler dispatch (email fan-outs, channel flips). Each
# transition is a discrete event the caller must act on exactly once.
# ─────────────────────────────────────────────────────────────────────


Transition = Literal[
    "entered_pre_warn",  # Crossed 90% — fire owner pre-warn email.
    "exhausted",  # Crossed 100% — fan-out user emails + flip preferences.
    "recovered",  # Was exhausted, now under 100% — fan-out "LINE is back" email.
]


class QuotaRefreshResult(NamedTuple):
    """What changed during a quota refresh. ``transitions`` is empty in
    the steady-state case (no thresholds crossed)."""

    limit: int | None
    used: int | None
    transitions: list[Transition]
    polled: bool  # False if the poll itself failed (network / non-2xx).


# ─────────────────────────────────────────────────────────────────────
# Raw LINE API client
# ─────────────────────────────────────────────────────────────────────


def fetch_line_quota() -> tuple[int, int] | None:
    """Query LINE for the current month's Push allowance + usage.

    Returns ``(limit, used)`` or ``None`` if the API call fails or the
    access token isn't configured. Both endpoints are free and don't
    count against the monthly Push cap.

    LINE's response shapes (Messaging API docs):

      ``GET /v2/bot/message/quota`` →
        ``{"type": "limited"|"none", "value": <int>}`` (``value`` only
        present when ``type == "limited"``; "none" means uncapped, which
        we treat as effectively unlimited by returning a sentinel).

      ``GET /v2/bot/message/quota/consumption`` →
        ``{"totalUsage": <int>}``
    """
    access_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "")
    if not access_token:
        logger.warning("line_quota: LINE_CHANNEL_ACCESS_TOKEN not configured")
        return None

    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        with httpx.Client(timeout=10) as http:
            quota_resp = http.get(f"{_LINE_API_BASE}/quota", headers=headers)
            cons_resp = http.get(f"{_LINE_API_BASE}/quota/consumption", headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("line_quota: poll failed (network): %s", exc)
        return None

    if not quota_resp.is_success or not cons_resp.is_success:
        logger.warning(
            "line_quota: poll failed (quota=%s, consumption=%s)",
            quota_resp.status_code,
            cons_resp.status_code,
        )
        return None

    quota_body = quota_resp.json()
    cons_body = cons_resp.json()

    quota_type = quota_body.get("type")
    if quota_type == "none":
        # Uncapped plan — sentinel value high enough that ratios never
        # cross thresholds. Polling still surfaces usage in the DB row.
        limit = 10_000_000
    elif quota_type == "limited":
        try:
            limit = int(quota_body.get("value", 0))
        except (TypeError, ValueError):
            logger.warning("line_quota: invalid quota.value %r", quota_body.get("value"))
            return None
    else:
        logger.warning("line_quota: unknown quota.type %r", quota_type)
        return None

    try:
        used = int(cons_body.get("totalUsage", 0))
    except (TypeError, ValueError):
        logger.warning("line_quota: invalid consumption.totalUsage %r", cons_body.get("totalUsage"))
        return None

    return limit, used


# ─────────────────────────────────────────────────────────────────────
# State refresh — single source of truth for transitions
# ─────────────────────────────────────────────────────────────────────


def refresh_quota_state() -> QuotaRefreshResult:
    """Poll LINE, persist the result to :class:`LineQuotaState`, and
    return any threshold transitions that crossed since the previous
    state. The caller (typically ``poll_line_quota_task``) is responsible
    for dispatching the per-tenant handlers.

    Transition semantics:

      - ``entered_pre_warn`` — usage just crossed
        :data:`PRE_WARN_THRESHOLD` from below. Reset (re-emitted) only
        after usage drops back below, which in practice means the next
        billing cycle.
      - ``exhausted`` — ``used >= limit`` and we were not exhausted
        previously. Sets ``line_quota_exhausted_at``.
      - ``recovered`` — was exhausted, now under cap. Clears
        ``line_quota_exhausted_at`` and ``line_quota_pre_warn_sent_at``
        (so the next month gets a fresh pre-warn event).
    """
    from apps.router.models import LineQuotaState

    polled = fetch_line_quota()
    state = LineQuotaState.get()

    if polled is None:
        return QuotaRefreshResult(
            limit=state.line_quota_limit,
            used=state.line_quota_used,
            transitions=[],
            polled=False,
        )

    limit, used = polled
    transitions: list[Transition] = []

    was_exhausted = state.is_exhausted
    prev_ratio = state.usage_ratio  # may be None on first poll
    new_ratio = used / limit if limit else 0.0

    state.line_quota_limit = limit
    state.line_quota_used = used
    state.line_quota_checked_at = timezone.now()

    # Pre-warn crossing — fire when we cross UP through the threshold.
    # First poll counts as a crossing if we're already over.
    if new_ratio >= PRE_WARN_THRESHOLD and (prev_ratio is None or prev_ratio < PRE_WARN_THRESHOLD):
        transitions.append("entered_pre_warn")

    # Exhaustion crossing.
    if used >= limit and not was_exhausted:
        state.line_quota_exhausted_at = timezone.now()
        transitions.append("exhausted")

    # Recovery — was exhausted but now under cap.
    if used < limit and was_exhausted:
        state.line_quota_exhausted_at = None
        # Clear pre-warn so next month gets a fresh signal.
        state.line_quota_pre_warn_sent_at = None
        transitions.append("recovered")

    state.save()
    return QuotaRefreshResult(limit=limit, used=used, transitions=transitions, polled=True)


# ─────────────────────────────────────────────────────────────────────
# Live tripwire — called from Push send paths on a 429
# ─────────────────────────────────────────────────────────────────────


def is_monthly_limit_429(status_code: int, body: str) -> bool:
    """True if a LINE Push response indicates monthly-cap exhaustion
    (not a generic rate-limit, not a transient 429)."""
    return status_code == 429 and _MONTHLY_LIMIT_SIGNAL in (body or "").lower()


def mark_quota_exhausted_from_429() -> bool:
    """Flip :class:`LineQuotaState` into the exhausted state immediately,
    without waiting for the next daily poll. Idempotent — repeated calls
    are no-ops once exhausted.

    Returns True iff this call performed the transition (so the caller
    can dispatch the exhaustion handler once).
    """
    from apps.router.models import LineQuotaState

    state = LineQuotaState.get()
    if state.is_exhausted:
        return False

    # If we have a known limit, force ``used`` to match so the row is
    # internally consistent. If we don't (no poll yet), leave used as-is
    # — the next poll will reconcile.
    if state.line_quota_limit:
        state.line_quota_used = state.line_quota_limit
    state.line_quota_exhausted_at = timezone.now()
    state.save()
    logger.warning("line_quota: marked exhausted via 429 tripwire")
    return True


# ─────────────────────────────────────────────────────────────────────
# Read-side helpers — used by the frontend gate + heartbeat gate
# ─────────────────────────────────────────────────────────────────────


def is_quota_exhausted() -> bool:
    """Cheap read of the singleton's exhausted flag. Safe to call from
    request handlers; one DB lookup."""
    from apps.router.models import LineQuotaState

    return LineQuotaState.get().is_exhausted


# ─────────────────────────────────────────────────────────────────────
# User-preferences flag helpers — track who we auto-flipped so we can
# email them when LINE comes back.
# ─────────────────────────────────────────────────────────────────────


def mark_user_flipped_by_quota(user: User) -> None:
    """Set ``preferences.channel_flipped_by_quota = True`` so the
    recovery handler can find this user later. Called immediately
    after flipping ``preferred_channel`` from line → telegram."""
    prefs = dict(user.preferences or {})
    prefs[USER_PREF_FLIPPED_BY_QUOTA] = True
    user.preferences = prefs
    user.save(update_fields=["preferences"])


def clear_user_flipped_flag(user: User) -> None:
    """Clear ``preferences.channel_flipped_by_quota``. Called after
    sending the recovery email — independent of whether the user
    accepts the prompt to switch back (we don't email them again next
    month if they decided to stay on Telegram)."""
    prefs = dict(user.preferences or {})
    if USER_PREF_FLIPPED_BY_QUOTA in prefs:
        del prefs[USER_PREF_FLIPPED_BY_QUOTA]
        user.preferences = prefs
        user.save(update_fields=["preferences"])


def was_user_flipped_by_quota(user: User) -> bool:
    prefs = user.preferences or {}
    return bool(prefs.get(USER_PREF_FLIPPED_BY_QUOTA))
