"""Proactive-outbound context: capture + surface for thread continuity.

When a cron-fired (or otherwise proactive) ``nbhd_send_to_user`` push
happens, the container often hibernates before the user replies. Their
reply then arrives on a fresh OpenClaw main-chat session that has no
memory of what was asked, so the agent can't anchor multi-paragraph
replies to the original question and conflates them.

This module is the deterministic fix:

* ``record_proactive_outbound`` is called from ``CronDeliveryView``
  after a successful Telegram/LINE/app delivery or eval-sink acceptance and
  persists a ``ProactiveOutbound`` row, keyed by the outbound transport or
  internal sink channel (telegram/line/app/eval) for audit.
* ``surface_proactive_context`` is called from each inbound path (LINE
  webhook, Telegram webhook, Telegram poller, and the iOS/app drain)
  and returns a marker block to prepend to the user's text so the agent
  sees the prior outbound(s) as conversation context.

Surfacing is TENANT-scoped and transport-agnostic across user-delivery
channels (Telegram, LINE, app); the internal eval channel is excluded. One
tenant is one human user on this platform, so a reply is threaded back to the
prior user-visible outbound(s) regardless of which transport recorded it. This is
deliberate — a cron delivered over Telegram while the user has since gone
iOS-only (Telegram still linked, so ``resolve_user_channel`` recorded the
row ``channel='telegram'``) must still surface when they reply from the
app. The ``channel`` / ``channel_user_id`` columns stay populated for
audit; they no longer scope the surface query.

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

from apps.common.eval_sink import suppresses_real_transport
from apps.router.models import ProactiveOutbound
from apps.router.reply_text import clamp_reply_text

logger = logging.getLogger(__name__)

# How far back to look when assembling context for a new inbound. A
# morning heartbeat at 06:30 and a user reply at 18:00 are still the
# same conversation; longer than a day risks pulling in genuinely stale
# context. Tunable per-deploy if we observe over- or under-surfacing.
DEFAULT_WINDOW_HOURS = 24

# How far back to look for UNCONSUMED rows. A proactive question that was
# never threaded into any conversation is the exact amnesia this module
# exists to prevent: if the user finally replies after two or three days,
# the agent must still see what it asked. So never-consumed rows stay
# surfaceable far longer than consumed ones (which keep the tight 24h
# window above) — re-surfacing an already-threaded message every turn would
# spam the conversation, but a never-answered one should persist until it
# is actually answered.
UNCONSUMED_WINDOW_HOURS = 168  # 7 days

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
    journal_link: dict | None = None,
    quick_replies: list[str] | None = None,
    artifact_dedup_key: str | None = None,
) -> ProactiveOutbound | None:
    """Persist a row describing one proactive delivery or eval evidence event.

    ``message_text`` is expected in PII-placeholder space (``[PERSON_1]``) — the
    space the agent authored it in. It is clamped and stored placeholder-space
    (pseudonymize-at-rest): ``message_text`` and its derived ``parsed_items``
    never hold real names at rest. Rehydration to real values happens only at
    owner-facing egress — the iOS APNs body below and the ``?since=`` feed
    builder (``chat_history._proactive_rows``). The model-facing reader
    (``surface_proactive_context``'s ``[earlier-from-you]`` block) reads the
    placeholder-space copy unchanged, which is correct — real names must not
    re-enter the agent turn.

    Failure to write must NOT fail the calling request — the message
    has already been delivered to the user; losing the audit row is a
    smaller wrong than 500ing the cron tool call. Errors are logged.
    """
    try:
        from apps.pii.egress import redact_known_values

        message_text = redact_known_values(tenant, message_text, seam="proactive_outbound_storage")
        with transaction.atomic():
            from apps.router.structured_artifacts import (
                externalize_large_structured_reply,
                proactive_artifact_dedup_key,
            )

            dedup_key = artifact_dedup_key or proactive_artifact_dedup_key(
                tenant=tenant,
                job_name=job_name,
                cleaned_message=message_text,
            )
            externalized = externalize_large_structured_reply(
                tenant=tenant,
                text=message_text,
                source="proactive",
                dedup_key=dedup_key,
                journal_link=journal_link,
            )
            message_text = clamp_reply_text(externalized.stored_text)
            journal_link = externalized.journal_link
            row = ProactiveOutbound.objects.create(
                tenant=tenant,
                channel=channel,
                channel_user_id=channel_user_id,
                message_text=message_text,
                job_name=(job_name or "")[:64],
                parsed_items=parse_markdown_items(message_text),
                # Parsed "View in Journal" deep-link (``{kind, slug, title}``, title
                # in placeholder space) — surfaced + rehydrated by the ?since= feed.
                # None on sends that carried no marker.
                journal_link=journal_link,
                # Parsed tap-to-send labels in placeholder space. They ride the
                # row across every delivery channel for the iOS feed and the
                # deterministic model-facing offer set below.
                quick_replies=quick_replies,
            )
    except Exception:
        logger.exception(
            "Failed to record ProactiveOutbound (tenant=%s channel=%s job=%s)",
            getattr(tenant, "id", "?"),
            channel,
            job_name or "-",
        )
        return None

    # An eval row is internal evidence, not a user-visible delivery. Do not even
    # invoke the push dispatcher: this keeps the sink independent of whether a
    # device token is later registered on the tenant.
    if channel == ProactiveOutbound.Channel.EVAL:
        return row

    # The tenant flag is authoritative.  A caller may have resolved a real
    # channel before an operator flips the tenant into eval-sink mode; preserve
    # the evidence row above, but never dispatch APNs from that stale channel.
    if suppresses_real_transport(tenant):
        return row

    # Ping the user's iPhone(s) that a proactive / cron message just landed — the
    # missing leg that left crons silent on iOS (Telegram/LINE delivered, but the
    # APNs push only ever fired for app-originated turns). The push is a
    # wake-and-sync trigger; the app pulls the text from the ?since= feed. This is
    # the single chokepoint every user-visible ProactiveOutbound funnels through,
    # so it covers both CronDeliveryView and core.services.notify_meditation_ready.
    # Eval evidence returned above never reaches this block. Dispatched
    # off the request path + fail-soft so an APNs send never delays or loses the
    # already-delivered cron message. The push body is OWNER-facing (lock screen)
    # so it rehydrates the placeholders — the stored ``message_text`` stays
    # placeholder-space, but the notification the user reads shows real names.
    from apps.pii.redactor import rehydrate_for_tenant

    _dispatch_ios_push(tenant, str(row.id), rehydrate_for_tenant(tenant, message_text))
    return row


def _dispatch_ios_push(tenant, proactive_id: str, body_source: str) -> None:
    """Fire the iOS APNs push for a just-recorded proactive row OFF the request
    path: an APNs send (≤10s timeout) must not delay the cron tool-call response.
    Synchronous under ``NBHD_DISABLE_BACKGROUND_THREADS`` (tests) for determinism.
    Mirrors ``apps.router.pending_queue._dispatch_push``. Fail-open."""

    def _run() -> None:
        try:
            from apps.router.push_views import notify_proactive_ready

            notify_proactive_ready(tenant, proactive_id, body_source)
        except Exception:
            logger.warning("proactive iOS push failed (non-fatal)", exc_info=True)

    try:
        from django.conf import settings

        if getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False):
            _run()
        else:
            import threading

            threading.Thread(target=_run, daemon=True).start()
    except Exception:
        logger.warning("proactive iOS push dispatch failed (non-fatal)", exc_info=True)


_STRUCTURED_GUIDANCE = (
    "[thread-rule: one of your earlier messages contained a numbered list. "
    "If the user's reply has the same number of paragraphs (or items), map "
    "each paragraph to the corresponding numbered item BY INDEX before "
    "interpreting topic. Out-of-order replies are common — anchor on "
    "structure first, content second.]\n"
)

# Always-on whenever ANY rows surface (unlike the structured thread-rule
# above, which needs parsed_items). Added after a 2026-07-11 canary
# incident: the Evening Check-in asked "where did energy land today,
# 1-10?", the user replied "6.5 today", the [earlier-from-you] block WAS
# surfaced — and the model still logged 6.5 as SLEEP HOURS via the fuel
# tools (a bare decimal at bedtime pattern-matched sleep instead of
# binding to the question shown). Rendered AFTER the message parts, right
# next to the user's reply, so "the messages above" reads literally.
# Kept to three sentences on purpose — this ships on every surfaced turn.
_ANSWER_BINDING_GUIDANCE = (
    "[answer-binding: the user's reply likely answers one of the messages "
    "above — bind it to the MOST RECENT question whose format fits before "
    "interpreting it as anything else. Match the answer to that question's "
    "scale and units: a reply to a 1-10 rating question is a RATING, not "
    "hours/quantity/money. If a bare number could answer more than one open "
    "question — or could be either an answer or a new log entry — ask one "
    "short clarifying question BEFORE writing to any log or tool.]\n"
)


def _format_block(rows: Iterable[ProactiveOutbound]) -> str:
    """Render the surfaced rows as a single marker block.

    The block goes BEFORE existing ``[chat: ...]`` / ``[Now: ...]``
    markers so the agent sees prior outbound context first and can
    treat the user's text as a reply to it. If any row has a non-empty
    ``parsed_items``, we render numbered anchors AND prepend a one-line
    ``[thread-rule: ...]`` guidance so the agent knows to map reply
    paragraphs by index when counts align. Every non-empty block also
    APPENDS the ``[answer-binding: ...]`` guidance after the message
    parts — adjacent to the user's reply — so a bare "6.5" binds to the
    1-10 question shown instead of pattern-matching a log entry.
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
        if row.journal_link:
            link = row.journal_link
            body += (
                f"\n[journal-ref: {link.get('kind', '')}|{link.get('slug', '')}|"
                f"{link.get('title', '')}; retrieve with nbhd_document_get]"
            )
        if row.quick_replies:
            offers = " | ".join(f'"{label}"' for label in row.quick_replies)
            body += f"\n(you offered quick replies: {offers})"
        parts.append(f"[earlier-from-you {when}{job}:\n{body}\n]")
    if not parts:
        return ""
    prefix = _STRUCTURED_GUIDANCE if any_structured else ""
    return prefix + "\n".join(parts) + "\n" + _ANSWER_BINDING_GUIDANCE


def surface_proactive_context(
    *,
    tenant,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    unconsumed_window_hours: int = UNCONSUMED_WINDOW_HOURS,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """Look up recent proactive outbounds for ``tenant`` and return a prepend block.

    EVAL-SINK TENANTS GET NOTHING — see the gate below.

    TENANT-scoped and transport-agnostic among user-delivery channels (see
    module docstring): a reply on ANY inbound path (iOS/app, Telegram, LINE)
    threads back to a user-visible proactive send that may have gone out over a
    different transport. Internal ``eval`` evidence rows are always excluded.

    The recency window is split by consumption state:

    * UNCONSUMED rows (never threaded into any turn) surface within the long
      ``unconsumed_window_hours`` (7 days). A proactive question the user
      leaves unanswered for days is exactly the amnesia this module exists to
      prevent — it must survive until they actually reply, not expire at 24h.
    * CONSUMED rows surface only within the tight ``window_hours`` (24h) AND a
      short follow-up window (5 min after consumption), so back-to-back replies
      in the same thread still see context — typical case: user replies
      "thanks", then a minute later sends the actual answer — without
      re-surfacing an old, already-threaded message on every subsequent turn.

    Selection is prioritized by consumption state, NOT pure recency:
    unconsumed rows fill the ``limit`` first (newest-first), and only the
    remaining slots go to consumed follow-up rows (newest-first). Pure
    recency would let a burst of fresh sends crowd a days-old unanswered
    question out of the cap — the exact row this window split protects.
    Never-threaded questions are the point of the module; consumed
    follow-ups are nice-to-have continuity. Marks the first-time-surfaced
    rows (both the 24h and the 7-day unconsumed ones) ``consumed_at = now``
    in the same DB transaction.

    Returns the empty string when there's nothing to surface.
    """
    # EVAL-SINK TENANTS: never surface anything into the model's context.
    #
    # This is the SECOND model-facing consumer of ProactiveOutbound (the USER.md
    # "Conversation so far" digest is the first, gated the same way in
    # conversation_capture.build_conversation_digest). It prepends
    # "[earlier-from-you]: <text>" onto inbound turns on EVERY ingress path,
    # including ``_drain_ios_batch`` — which is exactly the path the behavior
    # transport drives.
    #
    # It was harmless right up until the ``eval`` sink landed, because a synthetic
    # tenant had no ProactiveOutbound rows at all (everything 422'd). The sink
    # creates them: the eval tenant's daily system crons (Morning Briefing, Evening
    # Check-in) call nbhd_send_to_user and now record rows every single day. Without
    # this gate, the FIRST scenario turn of every nightly behavior run would drain
    # yesterday's briefing text into its prompt — and the reminder scenario could read
    # back its OWN previous water-nudge — while the run stamped ``isolated: True``.
    #
    # That is the same cross-turn contamination this PR exists to kill, walking back
    # in through the door the PR itself opened. Gate it at the function, so all four
    # ingress paths are covered at once.
    #
    # Sink rows remain unconsumed evidence. Operational readers exclude them;
    # eval probes and direct administrative inspection may still query them.
    if getattr(tenant, "is_eval_sink", False):
        return ""

    now = timezone.now()
    # Consumed rows keep the tight recency window; unconsumed rows get the
    # long one so a never-answered question stays surfaceable for days.
    consumed_cutoff = now - timedelta(hours=window_hours)
    unconsumed_cutoff = now - timedelta(hours=unconsumed_window_hours)
    # Short follow-up window for already-consumed rows. Five minutes
    # captures the "thanks, then the real reply" case without re-
    # surfacing a stale message forever.
    follow_up_cutoff = now - timedelta(minutes=5)

    # Tenant-scoped — the (tenant, created_at) index (proactive_tenant_created_idx)
    # backs both walks below. User-delivery channels are not distinguished: one
    # tenant = one human, and switching transports must not break continuity.
    # The internal eval evidence channel is the sole exclusion.
    base = ProactiveOutbound.objects.filter(tenant=tenant).exclude(channel=ProactiveOutbound.Channel.EVAL)

    # UNCONSUMED (never threaded) rows claim the limit first, newest-first,
    # within the long window.
    fresh = list(
        base.filter(
            consumed_at__isnull=True,
            created_at__gte=unconsumed_cutoff,
        ).order_by("-created_at")[:limit]
    )

    # Fill any remaining slots with consumed follow-up rows, newest-first:
    # created within 24h AND consumed within the 5-min follow-up window.
    fill = limit - len(fresh)
    if fill > 0:
        fresh += list(
            base.filter(
                consumed_at__isnull=False,
                created_at__gte=consumed_cutoff,
                consumed_at__gte=follow_up_cutoff,
            ).order_by("-created_at")[:fill]
        )

    if not fresh:
        return ""

    # A consumed fill row can be newer than a selected unconsumed one —
    # re-sort newest-first so the rendered block stays in strict
    # conversation order.
    fresh.sort(key=lambda r: r.created_at, reverse=True)

    # Mark first-time-surfaced rows consumed — a never-threaded question
    # (including the 7-day-old ones) is threaded exactly once.
    to_mark = [r.id for r in fresh if r.consumed_at is None]
    if to_mark:
        with transaction.atomic():
            ProactiveOutbound.objects.filter(id__in=to_mark).update(consumed_at=timezone.now())

    # Render oldest-first so the agent reads them in conversation order.
    return _format_block(reversed(fresh))
