"""Proactive-outbound context: capture + surface for thread continuity.

When a cron-fired (or otherwise proactive) ``nbhd_send_to_user`` push
happens, the container often hibernates before the user replies. Their
reply then arrives on a fresh OpenClaw main-chat session that has no
memory of what was asked, so the agent can't anchor multi-paragraph
replies to the original question and conflates them.

This module is the deterministic fix:

* ``record_proactive_outbound`` is called from ``CronDeliveryView``
  after a successful Telegram/LINE push and persists a
  ``ProactiveOutbound`` row.
* ``surface_proactive_context`` is called from each inbound envelope
  composer (LINE webhook, Telegram webhook, Telegram poller) and
  returns a marker block to prepend to the user's text so the agent
  sees the prior outbound(s) as conversation context.

The legacy ``_phase2_sync_block`` mechanism (which prompts the agent
to create a hidden ``_sync:`` cron) stays in place as a belt-and-
braces fallback; this module is the suspenders.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.router.models import ProactiveOutbound

logger = logging.getLogger(__name__)

# How far back to look when assembling context for a new inbound. A
# morning heartbeat at 06:30 and a user reply at 18:00 are still the
# same conversation; longer than a day risks pulling in genuinely stale
# context. Tunable per-deploy if we observe over- or under-surfacing.
DEFAULT_WINDOW_HOURS = 24

# Cap how many prior outbounds we surface. The common case is one
# proactive message → one reply. Three is enough to cover a "two
# crons fired before the user replied" tail.
DEFAULT_LIMIT = 3

# Markdown list-item patterns. Order matters — numbered first so a line
# like "1. foo" isn't matched as a sub-item of an outer bullet.
_NUMBERED_PATTERN = re.compile(r"^\s*\d+[.)]\s+(.+)$")
_BULLET_PATTERN = re.compile(r"^\s*[\-\*•]\s+(.+)$")


def parse_markdown_items(text: str) -> list[str]:
    """Extract top-level numbered or bulleted items from ``text``.

    Returns the visible item text (without the marker) preserving order.
    Returns an empty list when the text has no list structure or only a
    single item — a list of length 1 isn't a "structure" worth rendering.
    """
    items: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        m = _NUMBERED_PATTERN.match(line) or _BULLET_PATTERN.match(line)
        if m:
            items.append(m.group(1).strip())
    return items if len(items) >= 2 else []


def record_proactive_outbound(
    *,
    tenant,
    channel: str,
    channel_user_id: str,
    message_text: str,
    job_name: str = "",
) -> ProactiveOutbound | None:
    """Persist a row describing one successful proactive push.

    Failure to write must NOT fail the calling request — the message
    has already been delivered to the user; losing the audit row is a
    smaller wrong than 500ing the cron tool call. Errors are logged.
    """
    try:
        return ProactiveOutbound.objects.create(
            tenant=tenant,
            channel=channel,
            channel_user_id=channel_user_id,
            message_text=message_text,
            job_name=(job_name or "")[:64],
            parsed_items=parse_markdown_items(message_text),
        )
    except Exception:
        logger.exception(
            "Failed to record ProactiveOutbound (tenant=%s channel=%s job=%s)",
            getattr(tenant, "id", "?"),
            channel,
            job_name or "-",
        )
        return None


_STRUCTURED_GUIDANCE = (
    "[thread-rule: one of your earlier messages contained a numbered list. "
    "If the user's reply has the same number of paragraphs (or items), map "
    "each paragraph to the corresponding numbered item BY INDEX before "
    "interpreting topic. Out-of-order replies are common — anchor on "
    "structure first, content second.]\n"
)


def _format_block(rows: Iterable[ProactiveOutbound]) -> str:
    """Render the surfaced rows as a single marker block.

    The block goes BEFORE existing ``[chat: ...]`` / ``[Now: ...]``
    markers so the agent sees prior outbound context first and can
    treat the user's text as a reply to it. If any row has a non-empty
    ``parsed_items``, we render numbered anchors AND prepend a one-line
    ``[thread-rule: ...]`` guidance so the agent knows to map reply
    paragraphs by index when counts align.
    """
    parts: list[str] = []
    any_structured = False
    for row in rows:
        when_local = timezone.localtime(row.created_at) if row.created_at else None
        when = when_local.strftime("%Y-%m-%d %H:%M") if when_local else "earlier"
        job = f" job={row.job_name}" if row.job_name else ""
        if row.parsed_items:
            any_structured = True
            anchors = "\n".join(f"  [{i + 1}] {item}" for i, item in enumerate(row.parsed_items))
            body = f"{row.message_text}\n\n(numbered items you asked about:\n{anchors}\n)"
        else:
            body = row.message_text
        parts.append(f"[earlier-from-you {when}{job}:\n{body}\n]")
    if not parts:
        return ""
    prefix = _STRUCTURED_GUIDANCE if any_structured else ""
    return prefix + "\n".join(parts) + "\n"


def surface_proactive_context(
    *,
    tenant,
    channel: str,
    channel_user_id: str,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """Look up recent proactive outbounds and return a prepend block.

    Marks the surfaced rows ``consumed_at = now`` in the same DB
    transaction. Surfaces both unconsumed rows AND consumed-but-recent
    rows (within a short follow-up window) so that back-to-back replies
    in the same thread still see the same context — typical case: user
    replies "thanks", then a minute later sends the actual answer.

    Returns the empty string when there's nothing to surface.
    """
    cutoff = timezone.now() - timedelta(hours=window_hours)
    # Short follow-up window for already-consumed rows. Five minutes
    # captures the "thanks, then the real reply" case without re-
    # surfacing a stale message forever.
    follow_up_cutoff = timezone.now() - timedelta(minutes=5)

    qs = ProactiveOutbound.objects.filter(
        tenant=tenant,
        channel=channel,
        channel_user_id=channel_user_id,
        created_at__gte=cutoff,
    ).order_by("-created_at")[:limit]

    rows = list(qs)
    if not rows:
        return ""

    # Drop already-consumed rows whose consumption is outside the
    # follow-up window. Keep newer-first ordering.
    fresh: list[ProactiveOutbound] = []
    for row in rows:
        if row.consumed_at is None or row.consumed_at >= follow_up_cutoff:
            fresh.append(row)
    if not fresh:
        return ""

    # Mark first-time-surfaced rows consumed.
    to_mark = [r.id for r in fresh if r.consumed_at is None]
    if to_mark:
        with transaction.atomic():
            ProactiveOutbound.objects.filter(id__in=to_mark).update(consumed_at=timezone.now())

    # Render oldest-first so the agent reads them in conversation order.
    return _format_block(reversed(fresh))
