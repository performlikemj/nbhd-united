"""Deterministic conversation capture + the USER.md "Conversation so far" digest.

Background — the blindness this fixes
-------------------------------------
Telegram/LINE conversations are relayed to the per-tenant OpenClaw container and
never otherwise persisted in Postgres. Cron sessions (Evening Check-in,
Heartbeat, Morning Briefing, …) run in a SEPARATE, ISOLATED OpenClaw session
that cannot read the main chat transcript; the only "today" surfaces they can
read are written daily-note ``Document`` rows + ``nbhd_journal_context``, which
are empty unless the agent voluntarily journaled. So on a day with a substantive
chat (e.g. a job interview) that the agent didn't journal, every downstream cron
went blind and reported "quiet day on the chat front".

The fix is two halves, both in this module:

* :func:`record_conversation_turn` — called from the queue-drain chokepoints
  (``apps.router.pending_queue._drain_telegram_batch`` / ``_drain_line_batch``)
  and the Telegram webhook, after the reply is relayed. Fail-open: a lost audit
  row must never 500 a user reply. Persists a :class:`~apps.router.models.ConversationTurn`.
* :func:`build_conversation_digest` — rendered into USER.md via a registered
  envelope section. USER.md is auto-loaded by OpenClaw on EVERY agent turn, so
  the digest reaches even the isolated cheap-model crons that never call a tool.

iOS / web app chat is already durably persisted in ``AppChatMessage``; the digest
reads that table for the iOS slice rather than double-storing it here.

Mirrors the proven ``apps.router.proactive_context`` /
``apps.router.inbound_dedup`` patterns: capture-at-chokepoint, fail-open,
probabilistic self-pruning, debounced USER.md refresh.
"""

from __future__ import annotations

import logging
import random
import re
from datetime import timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from apps.router.models import ConversationTurn

logger = logging.getLogger(__name__)

# Self-pruning retention. 35 days covers the digest's "previous days" needs plus
# a possible weekly/monthly review, while keeping turns LESS persistent than the
# indefinitely-kept daily-note Documents they help mint.
_RETENTION = timedelta(days=35)
_PRUNE_PROBABILITY = 0.01
_PRUNE_BATCH = 500

# Per-turn storage caps. Bounds both at-rest size and digest token cost. The
# user side carries the topic signal; the reply is secondary context.
_USER_TEXT_MAX = 2000
_REPLY_TEXT_MAX = 800

# Leading-edge debounce for the USER.md refresh a captured turn triggers. The
# first turn of a chat burst pushes immediately (so today's conversation is
# visible), later turns within the window collapse into it. Crons fire hours
# later, so sub-window tail-staleness is irrelevant — we only need today's
# conversation present by evening. We deliberately do NOT wire ConversationTurn
# into the envelope registry's ``refresh_on`` because its universal receiver
# hardcodes ``debounce_seconds=0``, which would storm the fragile file share on
# the highest-frequency event in the system.
_REFRESH_DEBOUNCE_SECONDS = 120

# Strip OpenClaw inline markers from a stored reply so the digest is clean
# display text: ``[[chart:…]]`` / ``[[insight:…]]`` / ``[[button:…]]`` and any
# ``MEDIA:`` lines (workspace file paths the digest can't render).
_MARKER_RE = re.compile(r"\[\[[^\]]*\]\]")
_MEDIA_LINE_RE = re.compile(r"^\s*MEDIA:.*$", re.MULTILINE)


def clean_reply_for_capture(tenant, ai_text: str | None) -> str:
    """Rehydrate PII placeholders and strip inline markers from a raw reply.

    The container emits replies in PII-placeholder space (``[PERSON_1]``);
    rehydration to real names matches the chosen raw at-rest posture and keeps
    the digest readable. Does NOT record insights (that side effect belongs to
    the live relay path, not this audit capture). Fail-open on any error.
    """
    text = (ai_text or "").strip()
    if not text:
        return ""
    entity_map = getattr(tenant, "pii_entity_map", None)
    if entity_map:
        try:
            from apps.pii.redactor import rehydrate_text

            text = rehydrate_text(text, entity_map)
        except Exception:
            logger.exception("conversation_capture: PII rehydrate failed (non-fatal)")
    text = _MARKER_RE.sub("", text)
    text = _MEDIA_LINE_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def join_user_texts(rows) -> str:
    """Join the raw user excerpts of a (possibly coalesced) drain batch.

    Each ``PendingMessage.user_text`` is the user's undecorated text (no
    ``[Now:]`` / workspace markers — see ``poller._forward_to_container``).
    """
    parts = [(getattr(r, "user_text", "") or "").strip() for r in rows]
    return "\n".join(p for p in parts if p)


def record_conversation_turn(
    *,
    tenant,
    channel: str,
    channel_user_id: str,
    user_text: str,
    reply_text: str = "",
) -> ConversationTurn | None:
    """Persist one captured conversation turn. Fail-open — never raises.

    A turn with neither user nor reply text is dropped (nothing to record).
    On success, opportunistically prunes expired rows and schedules a debounced
    USER.md refresh so the digest is fresh before the next cron fires.
    """
    user_text = (user_text or "").strip()
    reply_text = (reply_text or "").strip()
    if not user_text and not reply_text:
        return None

    try:
        from apps.common.tenant_tz import tenant_today
        from apps.router.models import ConversationTurn

        row = ConversationTurn.objects.create(
            tenant=tenant,
            channel=channel,
            channel_user_id=(channel_user_id or "")[:128],
            local_date=tenant_today(tenant),
            user_text=user_text[:_USER_TEXT_MAX],
            reply_text=reply_text[:_REPLY_TEXT_MAX],
        )
    except Exception:
        logger.exception(
            "conversation_capture: record failed (tenant=%s channel=%s)",
            getattr(tenant, "id", "?"),
            channel,
        )
        return None

    _maybe_prune()
    schedule_user_md_refresh(tenant)
    return row


def _maybe_prune() -> None:
    """Opportunistically delete a bounded batch of expired turns."""
    if random.random() >= _PRUNE_PROBABILITY:
        return
    try:
        from apps.router.models import ConversationTurn

        cutoff = timezone.now() - _RETENTION
        stale_ids = list(
            ConversationTurn.objects.filter(created_at__lt=cutoff).values_list("id", flat=True)[:_PRUNE_BATCH]
        )
        if stale_ids:
            ConversationTurn.objects.filter(id__in=stale_ids).delete()
            logger.info("conversation_capture: pruned %d expired turns", len(stale_ids))
    except Exception:
        logger.exception("conversation_capture: prune pass failed (non-fatal)")


def schedule_user_md_refresh(tenant) -> None:
    """Schedule a debounced USER.md push so the digest reflects the new turn.

    Mirrors the registry's on-commit + background-thread shape, but with a real
    debounce window (see ``_REFRESH_DEBOUNCE_SECONDS``). Synchronous on_commit
    when background threads are disabled (tests/dev) for deterministic behavior.

    Public: also called by the on-device turn-record endpoint
    (``apps.router.chat_views.ChatLocalTurnView``) — an on-device turn changes
    the conversation digest exactly like a captured Telegram/LINE turn does.
    """
    tenant_id = str(getattr(tenant, "id", "") or "")
    if not tenant_id:
        return

    def _push() -> None:
        try:
            from apps.orchestrator.workspace_envelope import push_user_md

            push_user_md(tenant_id, debounce_seconds=_REFRESH_DEBOUNCE_SECONDS)
        except Exception:
            logger.warning(
                "conversation_capture: USER.md refresh failed for tenant %s",
                tenant_id[:8],
                exc_info=True,
            )

    from django.conf import settings
    from django.db import transaction

    if getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False):
        transaction.on_commit(_push)
    else:
        import threading

        transaction.on_commit(lambda: threading.Thread(target=_push, daemon=True).start())


# ---------------------------------------------------------------------------
# Digest rendering — sourced from ConversationTurn (telegram/line) +
# AppChatMessage (ios) so all channels are covered without double-storage.
# ---------------------------------------------------------------------------

# Token-budget knobs. USER.md has a ~12-18 KB bootstrap budget and is already
# truncation-prone, so the digest stays tight: a handful of today's lines plus a
# terse per-day rollup for the last few days.
_TODAY_MAX_LINES = 6
_TODAY_LINE_CHARS = 130
_PREV_DAYS = 3
_PREV_LINE_CHARS = 80


def _one_line(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return flat


def _collect_turns(tenant, *, since):
    """Unified, time-ordered turns from both sources within the window.

    Each item: ``{"dt": datetime, "date": local date, "user": str, "reply": str}``.
    """
    from apps.common.tenant_tz import tenant_tz
    from apps.router.models import AppChatMessage, ConversationTurn

    tz = tenant_tz(tenant)
    turns: list[dict] = []

    for t in ConversationTurn.objects.filter(tenant=tenant, created_at__gte=since).only(
        "created_at", "local_date", "user_text", "reply_text"
    ):
        turns.append({"dt": t.created_at, "date": t.local_date, "user": t.user_text, "reply": t.reply_text})

    for m in AppChatMessage.objects.filter(tenant=tenant, created_at__gte=since).only(
        "created_at", "user_text", "reply_text"
    ):
        turns.append(
            {
                "dt": m.created_at,
                "date": m.created_at.astimezone(tz).date(),
                "user": m.user_text,
                "reply": m.reply_text,
            }
        )

    turns.sort(key=lambda x: x["dt"])
    return turns, tz


def build_conversation_digest(tenant) -> str:
    """Render the body of the USER.md "Conversation so far" section.

    Returns ``""`` when there are no turns in the window (the registry then
    omits the section). Today is the tenant-local date; previous days are a
    terse per-day rollup. Deterministic — no LLM, no summarization.
    """
    from apps.common.tenant_tz import tenant_today

    today = tenant_today(tenant)
    since = timezone.now() - timedelta(days=_PREV_DAYS + 1)

    try:
        turns, tz = _collect_turns(tenant, since=since)
    except Exception:
        logger.exception("conversation_capture: digest collection failed (non-fatal)")
        return ""

    if not turns:
        return ""

    today_turns = [t for t in turns if t["date"] == today]
    horizon = today - timedelta(days=_PREV_DAYS)
    prev_turns = [t for t in turns if horizon <= t["date"] < today]

    lines: list[str] = [
        "_Recent chat with the user, captured across channels. Ground proactive "
        "turns in it — if turns appear here, the day was NOT quiet._",
    ]

    if today_turns:
        lines.append(f"\n**Today ({today.isoformat()}) · {len(today_turns)} message(s):**")
        for t in today_turns[-_TODAY_MAX_LINES:]:
            hhmm = t["dt"].astimezone(tz).strftime("%H:%M")
            user = _one_line(t["user"], _TODAY_LINE_CHARS)
            if user:
                lines.append(f"- {hhmm} — user: {user}")
            reply = _one_line(t["reply"], _TODAY_LINE_CHARS)
            if reply:
                lines.append(f"    ↳ you: {reply}")
    else:
        lines.append(f"\n**Today ({today.isoformat()}):** no messages yet.")

    if prev_turns:
        by_day: dict[str, list[dict]] = {}
        for t in prev_turns:
            by_day.setdefault(t["date"].isoformat(), []).append(t)
        lines.append("\n**Earlier this week:**")
        for day in sorted(by_day, reverse=True):
            day_turns = by_day[day]
            first_user = next(
                (_one_line(t["user"], _PREV_LINE_CHARS) for t in day_turns if (t["user"] or "").strip()), ""
            )
            tail = f' — "{first_user}…"' if first_user else ""
            lines.append(f"- {day} · {len(day_turns)} message(s){tail}")

    digest = "\n".join(lines)

    # Rehydrate placeholders (e.g. [PERSON_1]) so the USER.md envelope delivers
    # real-value text to the container, matching what ChatContextView already
    # does at render time (chat_views.py:430-437). Telegram ConversationTurn
    # user_text is stored redacted (poller.py:1375), so without this the digest
    # mixes placeholder user-lines with rehydrated reply-lines and raw iOS
    # user-lines — asymmetric and less useful for proactive grounding. Fail-open.
    entity_map = getattr(tenant, "pii_entity_map", None)
    if entity_map:
        try:
            from apps.pii.redactor import rehydrate_text

            digest = rehydrate_text(digest, entity_map)
        except Exception:
            logger.exception("conversation_capture: digest PII rehydrate failed (non-fatal)")

    return digest
