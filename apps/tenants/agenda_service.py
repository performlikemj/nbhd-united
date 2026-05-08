"""Service helpers for AgendaEngagement.

Single layer between callers (signal handlers, runtime endpoints, tests)
and the model. Keeps state-transition logic in one place so a thread's
lifecycle (nascent → introduced → active → dormant → abandoned/completed)
stays consistent regardless of which signal source updates it.

All helpers are safe to call concurrently — they use ``get_or_create``
+ ``update`` rather than read-then-write where possible. Append-only
``response_signals`` updates serialize via the row's update_at lock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .agenda_models import AgendaEngagement

if TYPE_CHECKING:
    from .models import Tenant


_SUPPRESS_AFTER_SURFACE = timedelta(hours=6)
"""How long a freshly-surfaced thread is hidden from re-rendering.

Tuned for the proactive-cron cadence: Heartbeat fires hourly, Morning
Briefing daily. Six hours is enough to skip the next 1-3 cron windows
after a fresh surface, then the thread becomes eligible again. This
threshold is the renderer's only soft-suppress signal — explicit state
(``ABANDONED`` / ``COMPLETED``) and ``surface_after`` are hard rules.
"""


def mark_surfaced(
    tenant: Tenant,
    *,
    kind: str,
    item_id: str,
    when: datetime | None = None,
    signal: str | None = None,
) -> AgendaEngagement:
    """Record that the assistant just surfaced this thread.

    Idempotent at the row level. Transitions ``state`` from NASCENT to
    INTRODUCED on first surface; later surfaces leave the state alone
    (downstream signals like response classification update state more
    decisively).

    ``signal`` (optional) is appended to ``response_signals`` so the
    surfacing event itself is captured in the log alongside any user
    response that follows.
    """
    when = when or datetime.now(UTC)
    obj, _created = AgendaEngagement.objects.get_or_create(
        tenant=tenant,
        kind=kind,
        item_id=item_id,
    )

    obj.last_surfaced_at = when
    if obj.state == AgendaEngagement.State.NASCENT:
        obj.state = AgendaEngagement.State.INTRODUCED
    if signal:
        obj.response_signals = list(obj.response_signals or []) + [
            {"at": when.isoformat(), "signal": signal, "kind": "surface"}
        ]
    obj.save()
    return obj


def record_signal(
    tenant: Tenant,
    *,
    kind: str,
    item_id: str,
    signal: str,
    when: datetime | None = None,
) -> AgendaEngagement:
    """Append a response signal to a thread's history.

    Vocabulary: ``warm`` (positive engagement), ``redirect`` (subject
    change), ``ignore`` (no response after surfacing), ``organic`` (user
    raised it first). Caller is free to use additional values; the log
    is JSON so downstream consumers can extend.

    Doesn't mutate ``state`` — that's the caller's call (or a separate
    ``mark_state`` invocation). Decoupling keeps signal capture cheap
    and lets state transitions remain explicit.
    """
    when = when or datetime.now(UTC)
    obj, _ = AgendaEngagement.objects.get_or_create(
        tenant=tenant,
        kind=kind,
        item_id=item_id,
    )
    obj.response_signals = list(obj.response_signals or []) + [{"at": when.isoformat(), "signal": signal}]
    obj.save(update_fields=["response_signals", "updated_at"])
    return obj


def mark_state(
    tenant: Tenant,
    *,
    kind: str,
    item_id: str,
    state: str,
) -> AgendaEngagement:
    """Explicit state transition.

    Valid states are members of ``AgendaEngagement.State``. Caller is
    responsible for choosing the right one — we don't enforce
    transition graphs, since legitimate paths exist between many pairs
    (e.g., dormant → active when the user re-engages organically).
    """
    if state not in AgendaEngagement.State.values:
        raise ValueError(f"unknown state {state!r}")

    obj, _ = AgendaEngagement.objects.get_or_create(
        tenant=tenant,
        kind=kind,
        item_id=item_id,
    )
    obj.state = state
    obj.save(update_fields=["state", "updated_at"])
    return obj


def defer_until(
    tenant: Tenant,
    *,
    kind: str,
    item_id: str,
    when: datetime,
) -> AgendaEngagement:
    """Set ``surface_after`` so the thread isn't re-surfaced until ``when``."""
    obj, _ = AgendaEngagement.objects.get_or_create(
        tenant=tenant,
        kind=kind,
        item_id=item_id,
    )
    obj.surface_after = when
    obj.save(update_fields=["surface_after", "updated_at"])
    return obj


# ---------------------------------------------------------------------------
# Renderer-facing helpers
# ---------------------------------------------------------------------------


def is_eligible_now(engagement: AgendaEngagement | None) -> bool:
    """Should this thread be rendered in the current agenda view?

    Returns True when there's no engagement row (no constraint) or
    when the row's state and timing allow surfacing. Renderer applies
    this filter per-thread.

    Hard skip:
      - state in {ABANDONED, COMPLETED}
      - surface_after in the future
      - last_surfaced_at within _SUPPRESS_AFTER_SURFACE (soft cooldown)
    """
    if engagement is None:
        return True

    if engagement.state in (
        AgendaEngagement.State.ABANDONED,
        AgendaEngagement.State.COMPLETED,
    ):
        return False

    now = datetime.now(UTC)
    if engagement.surface_after and engagement.surface_after > now:
        return False
    if engagement.last_surfaced_at and now - engagement.last_surfaced_at < _SUPPRESS_AFTER_SURFACE:
        return False
    return True


def engagements_by_item(
    tenant: Tenant,
    *,
    kind: str,
) -> dict[str, AgendaEngagement]:
    """Bulk fetch engagement rows for a tenant + kind, keyed by ``item_id``.

    Renderer pre-fetches once per kind to avoid N+1 queries when the
    section iterates over the underlying primitives.
    """
    return {e.item_id: e for e in AgendaEngagement.objects.filter(tenant=tenant, kind=kind)}
