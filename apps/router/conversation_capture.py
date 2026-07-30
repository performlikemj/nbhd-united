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

from apps.router.reply_text import clamp_reply_text

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
_LOCATION_COORDINATE_PAIR_RE = re.compile(r"(?<![\d.])[+-]?\d{1,3}\.\d+\s*,\s*[+-]?\d{1,3}\.\d+(?![\d.])")


def clean_reply_for_capture(tenant, ai_text: str | None) -> str:
    """Strip inline markers from a raw reply, keeping it in PII-placeholder space.

    The container emits replies in PII-placeholder space (``[PERSON_1]``). We
    persist that placeholder-space copy AS-IS (pseudonymize-at-rest): the stored
    ``ConversationTurn.reply_text`` never holds real names, and rehydration to
    real values happens only at the owner-facing read seam (the iOS ``?since=``
    feed, ``chat_history._conv_rows``). The USER.md "Conversation so far" digest
    (``build_conversation_digest``) is model-facing, so it reads this
    placeholder-space text unchanged — no real PII reaches the container prompt
    via this path.

    ``tenant`` is retained for signature stability (callers pass it uniformly).
    Does NOT record insights (that side effect belongs to the live relay path,
    not this audit capture). Fail-open on any error.
    """
    text = (ai_text or "").strip()
    if not text:
        return ""
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
            reply_text=clamp_reply_text(reply_text, max_chars=_REPLY_TEXT_MAX),
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
_OTHER_CHAT_TITLE_CHARS = 24
_OTHER_CHAT_INSTRUCTION = (
    "Lines tagged [other chat: …] are from the user's OTHER conversations — background context only; "
    "never present them as part of the current conversation, and attribute the source chat when referencing them."
)
# Per-line cap for the recent-proactive-sends dedup block (D2).
_PROACTIVE_LINE_CHARS = 130


def _one_line(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return flat


def _normalize_user_location_lines(text: str) -> str:
    """Remove decimal coordinates from recognized pin- or maps-bearing lines."""
    normalized = []
    for line in (text or "").splitlines():
        lower_line = line.lower()
        if "📍" in line or any(host in lower_line for host in ("maps.apple.com", "maps.google.com", "google.com/maps")):
            normalized.append(_LOCATION_COORDINATE_PAIR_RE.sub("…", line))
        else:
            normalized.append(line)
    return "\n".join(normalized)


def scrub_chat_thread_title(title: str) -> str:
    """Scrub a title through the shared location seam, with a safe empty-label fallback."""
    scrubbed = _normalize_user_location_lines(title)
    line_normalized_title = "\n".join((title or "").splitlines())
    if scrubbed == line_normalized_title:
        return title
    if not any(character.isalnum() for character in scrubbed):
        return "New chat"
    return scrubbed


def digest_thread_attribution_enabled(tenant: object | None) -> bool:
    """Return whether the shared digest should identify non-main iOS threads."""
    return tenant is not None and getattr(tenant, "digest_thread_attribution_enabled", False) is True


def _collect_turns(tenant, *, since):
    """Unified, time-ordered turns from both sources within the window.

    Each item: ``{"dt": datetime, "date": local date, "user": str, "reply": str,
    "other_chat_title": str}``.
    """
    from apps.common.tenant_tz import tenant_tz
    from apps.pii.redactor import redact_known_entities
    from apps.router import enc_columns, enc_read
    from apps.router.models import AppChatMessage, ConversationTurn

    tz = tenant_tz(tenant)
    turns: list[dict] = []

    for t in ConversationTurn.objects.filter(tenant=tenant, created_at__gte=since).only(
        "created_at", "local_date", "user_text", "reply_text"
    ):
        turns.append(
            {
                "dt": t.created_at,
                "date": t.local_date,
                "user": t.user_text,
                "reply": t.reply_text,
                "other_chat_title": "",
            }
        )

    # user_text_enc MUST be in .only() (plan risk #7): decrypting a deferred
    # field would trigger a per-row reload. build_since_page needs no such change
    # (it deliberately avoids .only()).
    attribution_enabled = digest_thread_attribution_enabled(tenant)
    app_msgs_qs = AppChatMessage.objects.filter(tenant=tenant, created_at__gte=since)
    if attribution_enabled:
        app_msgs_qs = app_msgs_qs.select_related("thread").only(
            "created_at",
            "user_text",
            "user_text_enc",
            "reply_text",
            "thread_id",
            "thread__is_main",
            "thread__title",
            "thread__title_enc",
        )
    else:
        app_msgs_qs = app_msgs_qs.only("created_at", "user_text", "user_text_enc", "reply_text")
    app_msgs = list(app_msgs_qs)
    # System-facing builder (feeds the USER.md digest AND the on-device
    # ChatContextView): decrypt user_text as SYSTEM — SILENT — even when an
    # owner_request is ambient, because this is a shared builder, not the owner
    # reading their own transcript (plan §2). One bulk read = one silent audit.
    app_user_texts = enc_read.read_values_bulk(
        tenant,
        enc_columns.APP_CHAT_MESSAGE_USER_TEXT,
        [(m.user_text_enc, m.user_text) for m in app_msgs],
        principal="system",
    )

    other_chat_titles = {}
    if attribution_enabled:
        other_threads = {}
        for message in app_msgs:
            if not message.thread.is_main:
                other_threads.setdefault(message.thread_id, message.thread)
        threads = list(other_threads.values())
        if threads:
            thread_titles = enc_read.read_values_bulk(
                tenant,
                enc_columns.CHAT_THREAD_TITLE,
                [(thread.title_enc, thread.title) for thread in threads],
                principal="system",
            )
            for thread, title in zip(threads, thread_titles):
                redacted_title = redact_known_entities(tenant, title.reveal())
                other_chat_titles[thread.id] = _one_line(redacted_title, _OTHER_CHAT_TITLE_CHARS) or "Untitled"

    for m, ut in zip(app_msgs, app_user_texts):
        turns.append(
            {
                "dt": m.created_at,
                "date": m.created_at.astimezone(tz).date(),
                # ``AppChatMessage.user_text`` is stored VERBATIM (the user's OWN
                # typed words, real values) so the owner-facing ``?since=`` feed can
                # echo exactly what was typed. But this digest is MODEL-facing —
                # USER.md is auto-loaded into every non-lightContext agent turn — so
                # a raw third-party name here leaks into the container prompt (and
                # then to the share via push_user_md). Reverse-map known names to
                # their placeholders against the tenant map (directive D3): a pure
                # dict + regex pass, reuse-only, that mints NOTHING. The iOS turn was
                # already redacted+minted once at ingress, so every name the detector
                # caught then already has a binding to reuse now; a NER-missed name
                # has no binding and is deliberately NOT coined here (no junk mint on
                # a render path, tenant.pii_entity_map is never mutated). No NER, no
                # DB write — safe on every debounced digest build.
                "user": redact_known_entities(tenant, ut.reveal()),
                # reply_text is placeholder-space at rest (pseudonymize-at-rest via
                # ``_store_ios_turn_reply``), so it is emitted unchanged. TODO
                # (directive D3): an ON_DEVICE / private-mode reply that stored real
                # values would need scrubbing here too; today the store path is
                # always placeholder-space, so no re-processing is required.
                "reply": m.reply_text,
                "other_chat_title": other_chat_titles.get(m.thread_id, ""),
            }
        )

    turns.sort(key=lambda x: x["dt"])
    return turns, tz


def _recent_proactive_lines(tenant, tz) -> list[str]:
    """Render the last few proactive sends into a compact dedup block (D2).

    Cron sessions and the heartbeat run as SEPARATE isolated OpenClaw sessions,
    so two routines firing the same morning each speak into apparent silence and
    can re-ask the same question. ``ProactiveOutbound`` records proactive
    delivery events and is read both on inbound replies and here for cron-to-cron
    dedup (internal eval evidence rows are excluded). By
    rendering "what you/a sibling routine already sent in the last 24h" into the
    same USER.md digest the isolated session already reads, a composing cron can
    see the heartbeat/briefing already asked it and not repeat.

    ``message_text`` is stored placeholder-space at rest (see
    ``record_proactive_outbound`` — pseudonymize-at-rest), so it is rendered
    unchanged; no scrub is needed here. Bounds reuse the inbound bridge's
    constants (24h window, 3 rows). Returns ``[]`` when nothing was sent
    in-window (the section is then omitted).
    """
    from apps.router.models import ProactiveOutbound
    from apps.router.proactive_context import DEFAULT_LIMIT, DEFAULT_WINDOW_HOURS

    since = timezone.now() - timedelta(hours=DEFAULT_WINDOW_HOURS)
    rows = list(
        ProactiveOutbound.objects.filter(tenant=tenant, created_at__gte=since)
        .exclude(channel=ProactiveOutbound.Channel.EVAL)
        .only("created_at", "message_text", "job_name")
        .order_by("-created_at")[:DEFAULT_LIMIT]
    )
    if not rows:
        return []

    lines = [
        "\n**Already sent to the user proactively (last 24h) — do NOT re-ask these in a proactive turn:**",
    ]
    for r in reversed(rows):  # oldest → newest so the block reads in time order
        hhmm = r.created_at.astimezone(tz).strftime("%H:%M")
        label = (r.job_name or "").strip() or "proactive"
        body = _one_line(r.message_text, _PROACTIVE_LINE_CHARS)
        lines.append(f"- {hhmm} · {label}: {body}")
    return lines


def build_conversation_digest(tenant, *, include_proactive_sends: bool = True) -> str:
    """Render the body of the USER.md "Conversation so far" section.

    Returns ``""`` when the window holds neither a captured turn NOR a recent
    proactive send (the registry then omits the section). Today is the
    tenant-local date; previous days are a terse per-day rollup; a trailing block
    replays recent proactive sends for cron-to-cron dedup (D2). Deterministic —
    no LLM, no summarization. Client digests set ``include_proactive_sends=False``
    because the device injects those rows from its local store.

    EVAL-SINK TENANTS GET NO DIGEST — see the guard below.
    """
    # This block is what silently defeated the behavior suite's per-scenario
    # isolation. The transport opens a FRESH ChatThread per scenario (its own
    # OpenClaw session, empty transcript) — and then the platform handed that
    # session the last N turns from EVERY thread ("captured across channels"), so
    # scenarios read each other's conversations anyway.
    #
    # Observed in production (behavior runs 72/79): the assistant opening a
    # manipulation scenario with "nice try AGAIN", and repeating its own prior
    # REFUSAL verbatim ("same answer as before — I don't have the ability to send
    # you a push notification") inside a thread that had never seen it. That made
    # a single bad turn self-reinforcing: once the reminder scenario failed, it
    # read its own refusal back and failed for the rest of the day.
    #
    # So the ``isolated: True`` flag every behavior result stamps was FALSE
    # PROVENANCE. Suppressing the digest here makes it true, and delivers what
    # docs/evals-directive.md §Suite 1 already promised: "a workspace reset between
    # runs so behavior runs never pollute".
    #
    # Consequence, stated rather than hidden: the behavior suite now measures
    # FIRST-CONTACT behavior — which is exactly what a new subscriber's first chat
    # is, and the case the reminder bug actually broke. A warm-continuity scenario
    # would need its own tenant with the digest left on.
    if getattr(tenant, "is_eval_sink", False):
        return ""

    from apps.common.tenant_tz import tenant_today

    today = tenant_today(tenant)
    since = timezone.now() - timedelta(days=_PREV_DAYS + 1)

    try:
        turns, tz = _collect_turns(tenant, since=since)
    except Exception:
        logger.exception("conversation_capture: digest collection failed (non-fatal)")
        return ""

    proactive_lines: list[str] = []
    if include_proactive_sends:
        try:
            proactive_lines = _recent_proactive_lines(tenant, tz)
        except Exception:
            logger.exception("conversation_capture: proactive digest block failed (non-fatal)")

    # Crons fire precisely when the user was quiet, so a proactive-only digest
    # (no captured turns) is still worth rendering — it is the sibling-cron dedup
    # signal. Omit the section only when BOTH sources are empty.
    if not turns and not proactive_lines:
        return ""

    lines: list[str] = []
    has_labeled_line = False

    if turns:
        today_turns = [t for t in turns if t["date"] == today]
        horizon = today - timedelta(days=_PREV_DAYS)
        prev_turns = [t for t in turns if horizon <= t["date"] < today]

        lines.append(
            "_Recent chat with the user, captured across channels. Ground proactive "
            "turns in it — if turns appear here, the day was NOT quiet._"
        )

        if today_turns:
            lines.append(f"\n**Today ({today.isoformat()}) · {len(today_turns)} message(s):**")
            for t in today_turns[-_TODAY_MAX_LINES:]:
                hhmm = t["dt"].astimezone(tz).strftime("%H:%M")
                other_chat_title = t["other_chat_title"]
                origin = f'[other chat: "{other_chat_title}"] ' if other_chat_title else ""
                user = _one_line(_normalize_user_location_lines(t["user"]), _TODAY_LINE_CHARS)
                if user:
                    lines.append(f"- {hhmm} — {origin}user: {user}")
                    has_labeled_line = has_labeled_line or bool(origin)
                reply = _one_line(t["reply"], _TODAY_LINE_CHARS)
                if reply:
                    lines.append(f"    ↳ {origin}you: {reply}")
                    has_labeled_line = has_labeled_line or bool(origin)
        else:
            lines.append(f"\n**Today ({today.isoformat()}):** no messages yet.")

        if prev_turns:
            by_day: dict[str, list[dict]] = {}
            for t in prev_turns:
                by_day.setdefault(t["date"].isoformat(), []).append(t)
            lines.append("\n**Earlier this week:**")
            for day in sorted(by_day, reverse=True):
                day_turns = by_day[day]
                first_user_turn = next(
                    (t for t in day_turns if (t["user"] or "").strip()),
                    None,
                )
                first_user = (
                    _one_line(_normalize_user_location_lines(first_user_turn["user"]), _PREV_LINE_CHARS)
                    if first_user_turn
                    else ""
                )
                other_chat_title = first_user_turn["other_chat_title"] if first_user_turn else ""
                origin = f'[other chat: "{other_chat_title}"] ' if other_chat_title else ""
                tail = f' — "{first_user}…"' if first_user else ""
                if origin:
                    tail = f' — {origin}"{first_user}…"'
                    has_labeled_line = True
                lines.append(f"- {day} · {len(day_turns)} message(s){tail}")

    lines.extend(proactive_lines)

    if has_labeled_line:
        lines.insert(0, _OTHER_CHAT_INSTRUCTION)

    digest = "\n".join(lines)

    # Deliberately NOT rehydrated. This digest is rendered into the USER.md
    # managed region, which is auto-loaded into the container/model prompt on
    # EVERY agent turn — a model-facing seam. Reply lines are stored in
    # PII-placeholder space (see ``clean_reply_for_capture`` /
    # ``_store_ios_turn_reply``), Telegram/LINE ``user_text`` is likewise
    # placeholder-space (poller.py), the proactive block reads placeholder-space
    # ``message_text`` at rest, and iOS ``AppChatMessage.user_text`` — the user's
    # OWN typed words, stored verbatim — is reverse-mapped to placeholders in
    # ``_collect_turns`` (directive D3). So the whole digest stays in placeholder
    # space and real third-party names never reach the model via this path — the
    # same posture the live redaction pipeline enforces on every inbound turn.
    # (The on-device ``ChatContextView`` digest IS owner-facing and rehydrates the
    # rendered context itself, so the scrub is transparent to the owner.)
    return digest
