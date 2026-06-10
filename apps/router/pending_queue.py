"""Per-tenant message serialization queue.

Why this exists
---------------

OpenClaw's ``claude`` CLI backend (subprocess-based, used for BYO Anthropic
Pro/Max) rejects concurrent turns on a single live session with
"Claude CLI live session is already handling a turn". Pre-#427 that meant
a silent fallback to MiniMax for the second message; post-#427 it means
the second message returns an error to the user. Either is broken UX for
any real conversation — and BYO Claude's first turn after a wake
regularly takes 30-150s (heavy MCP plugin tool use), which is enough time
for any human to send 2-3 follow-up messages.

This module serializes incoming webhook messages **per (tenant, channel,
channel_user_id)** so that:

  1. Message #1 from tenant T is forwarded to the container immediately,
     and a row is marked "in flight" for that key.
  2. Message #2 arrives WHILE #1 is in flight → enqueued, NOT forwarded.
     The webhook ACKs LINE/Telegram fast as usual.
  3. When #1's response comes back, the drain task fires #2 against the
     same live session (claude reuses the session via OpenClaw's
     ``--resume <sessionId>`` path, so context is preserved).

Hibernation buffering (PR #389 → PR #430) is a *separate* mechanism and
operates on ``BufferedMessage``. This queue (``PendingMessage``) covers
the **warm-tenant rapid-fire-messages** case.

Locking pattern
---------------

The drain task claims the next pending row inside a ``SELECT ... FOR
UPDATE SKIP LOCKED`` transaction with a soft ``delivery_in_flight_until``
lease, exactly like PR #430's pattern for ``BufferedMessage``. A
concurrent drain task observes the live lease and skips the row instead
of firing a duplicate ``/v1/chat/completions`` while the first turn is
mid-POST.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from typing import Any

import httpx
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from apps.billing.services import (
    record_usage,
    resolve_model_for_attribution,
)
from apps.router.models import PendingMessage
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


# Per-message attempt cap so a permanently-broken request can't wedge the
# queue forever. Mirrors ``_MAX_DELIVERY_ATTEMPTS`` in
# ``apps/orchestrator/hibernation.py``.
_MAX_DELIVERY_ATTEMPTS = 3

# Lease padding factor — see PR #430. Slightly more than the worst-case
# POST duration (timeout + backoffs) so a concurrent retry doesn't steal
# the row mid-flight, but bounded so a truly stuck row is freed on the
# next task tick.
_IN_FLIGHT_LEASE_FACTOR = 1.5

# QStash retry count for the per-message drain publish. Three failure
# modes can leave a row stuck PENDING with no follow-up drain — see the
# ``reap_stuck_inbound_messages_task`` docstring — and the reaper covers
# all three at 60s cadence. Three QStash retries here absorbs transient
# OC cold-start 504s without waiting the full minute for the reaper.
_DRAIN_PUBLISH_RETRIES = 3

# Seconds to wait after waking a hibernated container before re-attempting
# delivery. Idle hibernation deactivates the revision, so the OpenClaw
# container app returns 404 from its ingress until a fresh replica boots.
# Containers often boot in ~30s; a short defer plus the boot-grace retry
# below delivers within ~20s of readiness instead of always waiting the
# worst case (was a fixed 60s).
_WAKE_DEFER_SECONDS = 20

# After a wake, container-down errors within this window mean "still
# booting" — release the lease and retry in _WAKE_DEFER_SECONDS without
# advancing delivery_attempts (cap is only 3 and OpenClaw cold boots can
# take 30-150s; burning attempts during boot dropped slow-boot messages).
# Past the window a down container is treated as a real failure again.
_WAKE_BOOT_GRACE_SECONDS = 240

# Reaper sweep. Pending rows older than this with no live in-flight lease
# are presumed stuck (publish_task raised + got swallowed, or QStash
# delivered the drain task into the Django 5xx → DLQ pit, or a worker
# died mid-claim). The reaper republishes a fresh drain task per key —
# the drain's SKIP-LOCKED claim handles concurrency cleanly.
_REAPER_STUCK_AGE_SECONDS = 90

# Cap per reaper tick so a pathological backlog can't blow up the cron
# worker budget. At 60s cadence + 200 keys/tick the steady-state ceiling
# is ~3.3 republished drains/second across the entire fleet, which is
# well under QStash's free-tier rate limit.
_REAPER_BATCH_LIMIT = 200

# Stale-message guard. Any pending row claimed by the drain task whose
# created_at is older than this is dropped without POSTing to OC — the
# user's conversational frame has long since moved on, and the assistant
# would otherwise reply to a question they no longer remember asking
# (the canonical bug behind this module's reaper: see the 2026-05-23
# canary screenshot incident where two 7+h stale rows produced "this
# was already done" replies after the gateway recovered). We send a
# brief apology so the user knows what happened instead of receiving
# silent message loss.
_STALE_MESSAGE_AGE_SECONDS = 600  # 10 minutes

# Telegram bot API base — matches poller.py for consistency.
_TELEGRAM_API_BASE = "https://api.telegram.org/bot"

# Gateway error strings that should be treated as empty responses (the
# OpenClaw gateway sometimes returns a 200 with these in the message
# body when it can't reach the model).
_GATEWAY_ERROR_STRINGS: frozenset[str] = frozenset(
    {
        "No response from OpenClaw.",
        "No response from OpenClaw",
    }
)

# Substrings that indicate OpenRouter rejected a call due to the
# per-tenant sub-key's spending limit being exhausted (PR #1.6 Phase 4).
# OpenClaw 5.7's chat-completion handler wraps upstream exceptions in a
# generic "internal error" envelope (see openai-http-CtQN39Ne.js), so we
# also match on the inner error message when it leaks through. The match
# is case-insensitive substring on the JSON-serialised response body.
_OR_CREDIT_LIMIT_NEEDLES: tuple[str, ...] = (
    "insufficient credit",
    "insufficient_credit",
    "credit limit",
    "credit_limit",
    "quota exceeded",
    "quota_exceeded",
)


def _looks_like_openrouter_credit_limit(resp) -> bool:
    """Return True when a chat-completion response indicates the tenant's
    OpenRouter sub-key has hit its spending limit.

    Detection sources, most-specific first:
      1. HTTP 402 (Payment Required — the canonical OR signal)
      2. Any 4xx with a body string containing one of the credit-limit
         needles (OpenRouter's actual error text leaks through some
         OpenClaw error paths).
      3. HTTP 200 / 5xx whose JSON body has the same needles inside
         ``error.message`` or top-level ``message`` (OpenClaw 5.7's
         generic envelope when it preserves the upstream message).

    Conservative — false positives would cause spurious hibernation, so
    we require an explicit credit-limit string for the non-402 paths.
    """
    try:
        status = getattr(resp, "status_code", 0) or 0
        if status == 402:
            return True

        body_text = (getattr(resp, "text", "") or "").lower()
        if not body_text:
            return False
        if 400 <= status < 600:
            for needle in _OR_CREDIT_LIMIT_NEEDLES:
                if needle in body_text:
                    return True
        return False
    except Exception:
        # Defensive — never let detection itself blow up the drain task.
        return False


def _handle_openrouter_credit_limit(
    tenant: Tenant,
    *,
    channel: str,
    channel_user_id: str,
) -> None:
    """Trip the budget circuit breaker after OR rejected a chat call.

    1. Set ``estimated_cost_this_month`` to the tenant's effective cap so
       the existing ``check_budget`` short-circuit fires on the user's
       next inbound message.
    2. Hibernate the container via the existing ``_hibernate_for_quota``
       helper.
    3. Send a channel-appropriate budget-exhausted notification so the
       user sees an explanation instead of silence.

    PR #1.6 Phase 4. Called from the LINE + Telegram drain paths when
    ``_looks_like_openrouter_credit_limit`` returns True on the chat-
    completion response.
    """
    from decimal import Decimal

    from apps.router.error_messages import error_msg
    from apps.router.views import _hibernate_for_quota

    # Exempt-aware + credit-aware: a budget-exempt tenant (canary, internal) or a
    # tenant holding prepaid credit must NOT be hibernated when their per-tenant OR
    # key 402s at the included cap. Raise the key ceiling (exempt → high fixed cap,
    # credit → included+credit) and let them keep going — the soft gate
    # (check_budget) + record_usage debit enforce the precise balance, and reconcile
    # trues up. WITHOUT the exempt check here, an exempt tenant with $0 credit fell
    # straight through to hibernate + suspend (the 2026-06-10 canary outage).
    try:
        tenant.refresh_from_db(
            fields=["purchased_credit", "monthly_cost_budget", "model_tier", "openrouter_key_hash", "is_budget_exempt"]
        )
    except Exception:
        logger.exception("OR credit-limit: failed to refresh tenant=%s for credit check", str(tenant.id)[:8])
    if getattr(tenant, "is_budget_exempt", False) or getattr(tenant, "purchased_credit", Decimal("0")) > 0:
        try:
            from apps.billing.credits import sync_or_key_limit

            sync_or_key_limit(tenant)
            logger.info(
                "OR credit-limit: tenant=%s is budget-exempt or holds credit — raised key ceiling, NOT hibernating",
                str(tenant.id)[:8],
            )
            return
        except Exception:
            logger.exception(
                "OR credit-limit: ceiling re-raise failed for tenant=%s; falling through to hibernate",
                str(tenant.id)[:8],
            )

    try:
        cap = Decimal(str(tenant.effective_cost_budget))
        Tenant.objects.filter(id=tenant.id).update(estimated_cost_this_month=cap)
        tenant.estimated_cost_this_month = cap
    except Exception:
        logger.exception("OR credit-limit: failed to bump estimated_cost for tenant=%s", str(tenant.id)[:8])

    try:
        _hibernate_for_quota(tenant)
    except Exception:
        logger.exception("OR credit-limit: hibernate failed for tenant=%s", str(tenant.id)[:8])

    # PR #1.8: send the branded HTML cap-exhausted email so the tenant
    # has an inbox artifact explaining when chat resumes (the in-channel
    # text below is the immediate signal; the email is the durable one).
    # Idempotent — per-tenant sent-at marker on the Tenant row.
    try:
        from apps.router.billing_quota_handlers import send_cost_exhausted_email

        send_cost_exhausted_email(tenant)
    except Exception:
        logger.exception(
            "OR credit-limit: cap-exhausted email dispatch failed for tenant=%s",
            str(tenant.id)[:8],
        )

    lang = getattr(getattr(tenant, "user", None), "language", None) or "en"
    msg_key = "budget_exhausted_trial" if getattr(tenant, "is_trial", False) else "budget_exhausted_paid"
    frontend_url = str(getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org")).rstrip("/")
    text = error_msg(lang, msg_key, plus_message="", billing_url=f"{frontend_url}/billing")

    try:
        if channel == "line":
            from apps.router.line_webhook import _send_line_text

            _send_line_text(channel_user_id, text)
        elif channel == "telegram":
            try:
                chat_id_int = int(channel_user_id)
            except (TypeError, ValueError):
                logger.warning(
                    "OR credit-limit: invalid telegram chat_id %r for tenant=%s",
                    channel_user_id,
                    str(tenant.id)[:8],
                )
                return
            _send_telegram_markdown(chat_id_int, text)
    except Exception:
        logger.exception(
            "OR credit-limit: failed to send budget-exhausted message for tenant=%s channel=%s",
            str(tenant.id)[:8],
            channel,
        )

    logger.info(
        "OR credit-limit: tripped budget circuit for tenant=%s channel=%s — hibernated + cap-set + user notified",
        str(tenant.id)[:8],
        channel,
    )


def _resolve_chat_timeout(tenant: Tenant) -> float:
    """Return the per-attempt chat-completion timeout for a tenant.

    BYO Claude (anthropic/* via the bundled CLI) and reasoning models
    (Kimi K2.6) get the longer ``REASONING_MODEL_TIMEOUT`` because
    cold-start of the agent runtime + first-turn tool use regularly
    runs past the 120s default. Standard models keep
    ``DEFAULT_CHAT_TIMEOUT``. Both stay below the 300s gunicorn worker
    cap (CLAUDE.md gotcha).

    Mirrors ``_resolve_chat_timeout`` in ``apps/orchestrator/hibernation.py``
    (added in PR #430). Kept here as a tiny duplication rather than an
    import to keep the queue module self-contained and avoid a circular
    coupling between router → orchestrator → router.
    """
    from apps.billing.constants import (
        DEFAULT_CHAT_TIMEOUT,
        REASONING_MODEL_TIMEOUT,
        REASONING_MODELS,
    )

    # ``BYO_SLOW_MODELS`` was introduced in PR #430. Fall back gracefully
    # if it isn't present yet (so this PR can land before #430 without
    # breaking imports — they'll auto-merge once both are in).
    try:
        from apps.billing.constants import BYO_SLOW_MODELS
    except ImportError:  # pragma: no cover — defensive
        BYO_SLOW_MODELS: set[str] = set()

    model = (getattr(tenant, "preferred_model", "") or "").strip()
    if model in REASONING_MODELS or model in BYO_SLOW_MODELS:
        return REASONING_MODEL_TIMEOUT
    return DEFAULT_CHAT_TIMEOUT


# ---------------------------------------------------------------------------
# Public API — call from webhooks/poller
# ---------------------------------------------------------------------------


def enqueue_message_for_tenant(
    tenant: Tenant,
    channel: str,
    channel_user_id: str,
    payload: dict,
    user_text_excerpt: str = "",
) -> PendingMessage:
    """Insert a pending message row and schedule the drain task.

    The drain task is published with ``retries=_DRAIN_PUBLISH_RETRIES``
    (=3, QStash's default) so a transient OC cold-start 504 doesn't
    immediately drop the message into DLQ. Application-level guards
    still prevent duplicate work: the in-flight lease blocks overlapping
    POSTs, and the per-message attempt cap caps total work on a wedged
    row. Even if all three QStash attempts fail, the row sits PENDING
    and the per-minute reaper (``reap_stuck_inbound_messages_task``)
    republishes a fresh drain within ~60s.

    Returns the freshly created ``PendingMessage`` row so callers can
    log / inspect.

    NOTE: webhooks must do channel-specific preprocessing (workspace
    routing, datetime context injection, PII redaction, etc.) BEFORE
    enqueuing. The queue is dumb on purpose — it just POSTs the
    prepared payload at the container and relays the reply. That keeps
    the drain task channel-agnostic and avoids re-resolving routing at
    drain time, when the user might already be in a different
    workspace.
    """
    msg = PendingMessage.objects.create(
        tenant=tenant,
        channel=channel,
        channel_user_id=channel_user_id or "",
        payload=payload,
        user_text=user_text_excerpt or "",
    )

    # Stamp first_message_at on the tenant's first-ever inbound. Conditional
    # UPDATE so concurrent first messages from a never-messaged tenant don't
    # race-overwrite an earlier timestamp; the filter makes the second write
    # a no-op. This is the activation signal used to measure the onboarding
    # drop-off cohort.
    Tenant.objects.filter(id=tenant.id, first_message_at__isnull=True).update(first_message_at=timezone.now())

    try:
        from apps.cron.publish import publish_task

        publish_task(
            "drain_pending_messages_for_tenant",
            str(tenant.id),
            channel,
            channel_user_id or "",
            retries=_DRAIN_PUBLISH_RETRIES,
        )
    except Exception:
        # Reaper safety net: the per-minute cron picks up rows whose
        # initial publish failed and republishes the drain. So a silent
        # publish failure here means a ~60s delay, not a multi-hour stall
        # (the historical failure mode this module was rewritten to fix).
        logger.exception(
            "pending_queue: failed to publish drain task for tenant %s — reaper will pick up row %s within ~60s",
            str(tenant.id)[:8],
            msg.id,
        )

    return msg


# ---------------------------------------------------------------------------
# Internal: claim + drain
# ---------------------------------------------------------------------------


def _row_is_voice(msg: PendingMessage) -> bool:
    """A row is "voice" if its payload carries ``is_voice=True``.

    Voice rows are excluded from cold-start coalescing — they're a
    different content shape (transcribed audio with its own prefix in
    OpenClaw) and shouldn't get folded into a multi-message text bundle.
    """
    payload = msg.payload or {}
    return bool(payload.get("is_voice"))


def _claim_pending_batch_for_key(
    tenant: Tenant,
    channel: str,
    channel_user_id: str,
    timeout_seconds: float,
) -> tuple[list[PendingMessage], dict]:
    """Claim a deliverable head-of-queue batch for the given key.

    Returns ``(batch, info)`` where ``batch`` is an ordered list (oldest →
    newest) of rows with fresh leases, and ``info`` is a small dict for
    the caller to handle head-of-queue drops:

      - ``info["past_cap_head"]``: head row is past the attempts cap and
        must be dropped + apologized. No lease is taken.
      - ``info["stale_head"]``: head row is older than
        ``_STALE_MESSAGE_AGE_SECONDS`` — lease IS taken so caller can
        atomically flip ``status=FAILED`` and clear the lease without a
        concurrent drain racing for the same row.

    Batch composition rules (preserves the per-key single-turn invariant
    that prevents the OpenClaw claude-cli backend from rejecting
    overlapping turns):

      - Always starts with the oldest unleased PENDING row.
      - Subsequent contiguous rows are folded in as long as they are
        fresh (under stale threshold), under the attempts cap, and not
        voice. The batch breaks at the first row that fails any of
        these — that row stays PENDING and gets handled on the next
        drain tick.
      - Voice rows are always singletons: if the head row is voice, the
        batch is ``[voice_row]``; otherwise voice rows in the tail end
        the batch.

    All claim + lease writes happen inside one
    ``SELECT ... FOR UPDATE SKIP LOCKED`` transaction so a concurrent
    drain task that observes a leased head row also sees the rest of
    the batch as leased — no two drain tasks ever build overlapping
    batches for the same key.
    """
    lease_seconds = timeout_seconds * _IN_FLIGHT_LEASE_FACTOR

    with transaction.atomic():
        now = timezone.now()
        stale_cutoff = now - timedelta(seconds=_STALE_MESSAGE_AGE_SECONDS)

        # Per-key single-turn invariant: if ANY row for this key already
        # carries a live in-flight lease, a concurrent drain task is
        # mid-POST for that key. We must NOT claim any other rows for
        # the same (tenant, channel, channel_user_id) while that POST
        # is in flight — overlapping ``/v1/chat/completions`` calls into
        # the same OpenClaw session trigger the Claude CLI's "live
        # session is already handling a turn" rejection. Pre-coalesce,
        # this invariant was weaker (only same-row was guarded by
        # SKIP LOCKED, not same-key); coalescing strengthens it so
        # follow-up messages naturally fall into the next batch instead
        # of racing the in-flight turn.
        has_live_lease = PendingMessage.objects.filter(
            tenant=tenant,
            channel=channel,
            channel_user_id=channel_user_id or "",
            delivery_status=PendingMessage.Status.PENDING,
            delivery_in_flight_until__gt=now,
        ).exists()
        if has_live_lease:
            return ([], {})

        qs = (
            PendingMessage.objects.select_for_update(skip_locked=True)
            .filter(
                tenant=tenant,
                channel=channel,
                channel_user_id=channel_user_id or "",
                delivery_status=PendingMessage.Status.PENDING,
            )
            .filter(models.Q(delivery_in_flight_until__isnull=True) | models.Q(delivery_in_flight_until__lt=now))
            .order_by("created_at")
        )
        rows = list(qs)
        if not rows:
            return ([], {})

        head = rows[0]

        # Past-cap head → caller drops + apologizes. No lease.
        if head.delivery_attempts >= _MAX_DELIVERY_ATTEMPTS:
            return ([], {"past_cap_head": head})

        # Stale head → take lease so the FAILED-flip is uncontended.
        if head.created_at < stale_cutoff:
            head.delivery_in_flight_until = now + timedelta(seconds=lease_seconds)
            head.save(update_fields=["delivery_in_flight_until"])
            return ([], {"stale_head": head})

        # Voice head → singleton batch (voice is never coalesced).
        if _row_is_voice(head):
            head.delivery_in_flight_until = now + timedelta(seconds=lease_seconds)
            head.save(update_fields=["delivery_in_flight_until"])
            return ([head], {})

        # Build a contiguous head batch of fresh, under-cap, non-voice rows.
        batch: list[PendingMessage] = [head]
        for row in rows[1:]:
            if row.delivery_attempts >= _MAX_DELIVERY_ATTEMPTS:
                break
            if row.created_at < stale_cutoff:
                break
            if _row_is_voice(row):
                break
            batch.append(row)

        for row in batch:
            row.delivery_in_flight_until = now + timedelta(seconds=lease_seconds)
            row.save(update_fields=["delivery_in_flight_until"])

        return (batch, {})


def _has_more_pending(tenant: Tenant, channel: str, channel_user_id: str) -> bool:
    """Cheap check for whether any more pending rows exist for this key.

    Used after a successful drain to decide whether to re-schedule the
    drain task immediately (more work) or exit.
    """
    return PendingMessage.objects.filter(
        tenant=tenant,
        channel=channel,
        channel_user_id=channel_user_id or "",
        delivery_status=PendingMessage.Status.PENDING,
    ).exists()


def drain_pending_messages_for_tenant_task(
    tenant_id: str,
    channel: str,
    channel_user_id: str,
) -> dict:
    """Drain the next pending message for ``(tenant, channel, channel_user_id)``.

    Called via QStash ~immediately after ``enqueue_message_for_tenant``,
    and re-scheduled by itself when more rows remain in the queue.

    Resilience semantics (parallel to ``deliver_buffered_messages_task``):
      - Each row is claimed inside a SELECT ... FOR UPDATE SKIP LOCKED
        transaction with a soft ``delivery_in_flight_until`` lease, so a
        concurrent drain invocation can't re-fire ``/v1/chat/completions``
        while the first POST is still running.
      - Per-attempt timeout adapts to the tenant's preferred model: BYO
        Claude (via the claude CLI backend) and reasoning models get the
        ``REASONING_MODEL_TIMEOUT`` (240s) instead of the 120s default.
      - On a real per-message failure we increment ``delivery_attempts``
        and stop — the next drain tick (QStash retry, or another
        webhook arrival) will retry. Once a message hits
        ``_MAX_DELIVERY_ATTEMPTS`` we mark it ``status=failed`` and move
        on so a permanently broken request can't wedge the queue forever.

    Returns a small dict for logging/testing.
    """
    tenant = Tenant.objects.select_related("user").filter(id=tenant_id).first()
    if not tenant or not tenant.container_fqdn:
        logger.warning(
            "drain_pending: tenant %s not found or no FQDN, dropping queue",
            tenant_id[:8],
        )
        # Defensive cleanup: mark any orphaned rows as failed so we don't
        # endlessly re-schedule against a tenant that's been deprovisioned.
        PendingMessage.objects.filter(
            tenant_id=tenant_id,
            channel=channel,
            channel_user_id=channel_user_id or "",
            delivery_status=PendingMessage.Status.PENDING,
        ).update(
            delivery_status=PendingMessage.Status.FAILED,
            delivered_at=timezone.now(),
            delivery_in_flight_until=None,
        )
        return {"delivered": 0, "failed": 0, "dropped": 0, "skipped_in_flight": 0}

    chat_timeout = _resolve_chat_timeout(tenant)
    batch, info = _claim_pending_batch_for_key(tenant, channel, channel_user_id or "", chat_timeout)

    # Past-cap head — no lease taken; drop + apologize + reschedule if more.
    past_cap_head = info.get("past_cap_head")
    if past_cap_head is not None:
        logger.warning(
            "drain_pending: dropping msg %s for tenant %s after %d attempts",
            past_cap_head.id,
            tenant_id[:8],
            past_cap_head.delivery_attempts,
        )
        past_cap_head.delivery_status = PendingMessage.Status.FAILED
        past_cap_head.delivered_at = timezone.now()
        past_cap_head.delivery_in_flight_until = None
        past_cap_head.save(
            update_fields=[
                "delivery_status",
                "delivered_at",
                "delivery_in_flight_until",
            ]
        )
        _send_apology_for_dropped_pending_message(tenant, past_cap_head)
        if _has_more_pending(tenant, channel, channel_user_id or ""):
            _reschedule_drain(tenant, channel, channel_user_id or "")
        return {"delivered": 0, "failed": 0, "dropped": 1, "skipped_in_flight": 0}

    # Stale head — lease IS already taken by the batch claim; flip to
    # FAILED and apologize. The user's conversational frame has moved on
    # so delivering now produces "responding to questions from hours ago"
    # UX bug this module was rewritten to fix.
    stale_head = info.get("stale_head")
    if stale_head is not None:
        msg_age_seconds = (timezone.now() - stale_head.created_at).total_seconds()
        logger.warning(
            "drain_pending: msg %s for tenant %s is stale (age=%ds > %ds), "
            "marking failed without OC POST and sending apology",
            stale_head.id,
            tenant_id[:8],
            int(msg_age_seconds),
            _STALE_MESSAGE_AGE_SECONDS,
        )
        stale_head.delivery_status = PendingMessage.Status.FAILED
        stale_head.delivered_at = timezone.now()
        stale_head.delivery_in_flight_until = None
        stale_head.save(
            update_fields=[
                "delivery_status",
                "delivered_at",
                "delivery_in_flight_until",
            ]
        )
        _send_apology_for_stale_pending_message(tenant, stale_head, msg_age_seconds)
        if _has_more_pending(tenant, channel, channel_user_id or ""):
            _reschedule_drain(tenant, channel, channel_user_id or "")
        return {"delivered": 0, "failed": 0, "dropped": 1, "skipped_in_flight": 0, "stale": 1}

    if not batch:
        # Either the key's queue is drained or every remaining row has
        # a live in-flight lease held by a concurrent task. Either way
        # this run has nothing more to do — bail without erroring so we
        # don't trigger another QStash retry that would just hit the
        # same lease.
        held_count = PendingMessage.objects.filter(
            tenant=tenant,
            channel=channel,
            channel_user_id=channel_user_id or "",
            delivery_status=PendingMessage.Status.PENDING,
        ).count()
        if held_count:
            logger.info(
                "drain_pending: tenant %s key=%s/%s — %d msg(s) held by "
                "concurrent in-flight lease, letting that task complete",
                tenant_id[:8],
                channel,
                (channel_user_id or "")[:24],
                held_count,
            )
        return {
            "delivered": 0,
            "failed": 0,
            "dropped": 0,
            "skipped_in_flight": held_count,
        }

    batch_size = len(batch)
    if batch_size > 1:
        logger.info(
            "drain_pending: tenant %s key=%s/%s — coalescing %d messages into one OC turn (cold-start coalesce)",
            tenant_id[:8],
            channel,
            (channel_user_id or "")[:24],
            batch_size,
        )

    delivered = 0
    failed = 0
    gateway_responded = False
    try:
        if channel == PendingMessage.Channel.LINE:
            gateway_responded = _drain_line_batch(tenant, batch, chat_timeout)
        elif channel == PendingMessage.Channel.TELEGRAM:
            gateway_responded = _drain_telegram_batch(tenant, batch, chat_timeout)
        elif channel == PendingMessage.Channel.IOS:
            gateway_responded = _drain_ios_batch(tenant, batch, chat_timeout)
        else:
            raise ValueError(f"Unknown channel: {channel!r}")

        now = timezone.now()
        for row in batch:
            row.delivery_status = PendingMessage.Status.DELIVERED
            row.delivered_at = now
            row.delivery_in_flight_until = None
            row.save(
                update_fields=[
                    "delivery_status",
                    "delivered_at",
                    "delivery_in_flight_until",
                ]
            )
        delivered = batch_size

    except Exception as exc:
        # Hibernated container on the poller path. The Telegram poller
        # (apps/router/poller.py) enqueues straight to PendingMessage with
        # no hibernation check — unlike the webhook handlers (views.py /
        # line_webhook.py) which route a hibernated tenant's message through
        # ``handle_hibernated_message`` → ``wake_hibernated_tenant``. Idle
        # hibernation deactivates the revision, so this POST 404s; without
        # the branch below the batch would burn all _MAX_DELIVERY_ATTEMPTS
        # in ~2 min and be DROPPED, and nothing would ever wake the
        # container — the user's "wake me" message is silently lost
        # (canary 148ccf1c, 2026-06-05). Wake the container and defer the
        # drain instead. Release the lease but DON'T advance the attempt
        # counter — the message did nothing wrong, the container was asleep;
        # genuine post-wake failures still hit the cap on the deferred
        # re-drain. Gated on a 404 (the deactivated-revision signature) so a
        # transient 5xx from a live-but-stale-flagged container still flows
        # through the normal retry path (and the reconcile below).
        if tenant.hibernated_at is not None and _is_container_down_error(exc):
            from apps.billing.services import check_budget

            # Mirror the webhook's "budget check before wake": never re-wake
            # a tenant hibernated for being over budget — that would un-gate
            # spend. Over-budget messages fall through to the normal
            # attempt-cap path (drop + apologize).
            if not check_budget(tenant):
                from apps.orchestrator.hibernation import wake_hibernated_tenant

                for row in batch:
                    row.delivery_in_flight_until = None
                    row.save(update_fields=["delivery_in_flight_until"])
                logger.info(
                    "drain_pending: tenant %s hibernated (container 404) — waking and deferring drain %ds",
                    tenant_id[:8],
                    _WAKE_DEFER_SECONDS,
                )
                wake_hibernated_tenant(tenant)
                _notify_waking(tenant, channel, channel_user_id or "")
                _mark_ios_waking(channel, batch)
                _reschedule_drain(
                    tenant,
                    channel,
                    channel_user_id or "",
                    delay_seconds=_WAKE_DEFER_SECONDS,
                )
                return {
                    "delivered": 0,
                    "failed": 0,
                    "dropped": 0,
                    "skipped_in_flight": 0,
                    "woke": True,
                }

        # Boot grace: the container was woken moments ago (this drain or the
        # webhook path) and its replica isn't serving yet. Not the message's
        # fault — release the lease, keep the attempt counters, retry soon.
        # Without this, the shorter _WAKE_DEFER_SECONDS would burn all
        # _MAX_DELIVERY_ATTEMPTS during a slow cold boot.
        if (
            _is_container_down_error(exc)
            and tenant.last_wake_at is not None
            and (timezone.now() - tenant.last_wake_at).total_seconds() < _WAKE_BOOT_GRACE_SECONDS
        ):
            for row in batch:
                row.delivery_in_flight_until = None
                row.save(update_fields=["delivery_in_flight_until"])
            logger.info(
                "drain_pending: tenant %s still booting after wake — deferring drain %ds (no attempt burned)",
                tenant_id[:8],
                _WAKE_DEFER_SECONDS,
            )
            _mark_ios_waking(channel, batch)
            _reschedule_drain(
                tenant,
                channel,
                channel_user_id or "",
                delay_seconds=_WAKE_DEFER_SECONDS,
            )
            return {
                "delivered": 0,
                "failed": 0,
                "dropped": 0,
                "skipped_in_flight": 0,
                "booting": True,
            }

        # Batch-level failure: every row in the batch shared the failed
        # POST, so every row's attempt counter advances by one. A row
        # hitting the cap will be dropped+apologized on the next drain
        # tick (it'll surface via ``past_cap_head``); rows still under
        # cap will retry as a (possibly smaller) batch.
        logger.exception(
            "drain_pending: failed to deliver batch of %d for tenant %s (rows %s, attempts now %d-%d/%d)",
            batch_size,
            tenant_id[:8],
            ",".join(str(r.id)[:8] for r in batch),
            batch[0].delivery_attempts + 1,
            batch[-1].delivery_attempts + 1,
            _MAX_DELIVERY_ATTEMPTS,
        )
        for row in batch:
            row.delivery_attempts += 1
            row.delivery_in_flight_until = None
            row.save(
                update_fields=[
                    "delivery_attempts",
                    "delivery_in_flight_until",
                ]
            )
        failed = batch_size

    # Reconcile a stale hibernation flag. A healthy (non-credit-limit)
    # gateway response is proof the container is awake, so a lingering
    # ``hibernated_at`` is stale and must be cleared — otherwise
    # ``apps.router.container_updates.update_container`` keeps short-
    # circuiting (its ``if tenant.hibernated_at: return False`` guard) and
    # every self-update attempt reports "the update failed", and idle
    # accounting keeps treating a live tenant as asleep. This is the warm-
    # path analogue of ``wake_hibernated_tenant``'s clear: the *webhook*
    # path clears the flag on wake, but the Telegram *poller* path has no
    # wake step (poller → enqueue → this drain, no ``handle_hibernated_message``),
    # so an out-of-band revision activate (e.g. a manual ``az containerapp``
    # image swap) leaves the flag set indefinitely while the tenant chats
    # normally. Done OUTSIDE the delivery try/except so a flag-clear hiccup
    # can't flip a successful delivery to "failed"; ``gateway_responded`` is
    # False for the credit-limit early-return, which intentionally
    # hibernates — so we never undo a just-applied budget hibernation.
    if delivered and gateway_responded and tenant.hibernated_at is not None:
        try:
            Tenant.objects.filter(id=tenant.id).update(hibernated_at=None)
            tenant.hibernated_at = None
            logger.info(
                "drain_pending: cleared stale hibernated_at for tenant %s (live gateway response on %s)",
                tenant_id[:8],
                channel,
            )
        except Exception:
            logger.exception(
                "drain_pending: failed to clear stale hibernated_at for tenant %s",
                tenant_id[:8],
            )

    # On success: if more pending rows remain for this key, schedule the
    # next drain immediately so back-to-back messages keep flowing.
    #
    # On failure we deliberately do NOT re-schedule. The QStash retry
    # (``retries=_DRAIN_PUBLISH_RETRIES``) handles second-chance attempts
    # with QStash's natural backoff, and the per-message
    # ``delivery_attempts`` counter still caps total attempts at
    # ``_MAX_DELIVERY_ATTEMPTS``. Re-scheduling here would synchronously
    # cascade through the cap in tests and burn the attempts budget on a
    # request that's almost certainly going to keep failing.
    if delivered and _has_more_pending(tenant, channel, channel_user_id or ""):
        _reschedule_drain(tenant, channel, channel_user_id or "")

    if failed:
        # Surface a non-2xx so QStash retries the task. The
        # application-level lease + attempt cap prevents this from
        # spawning a duplicate POST against the container.
        raise RuntimeError(
            f"drain_pending: batch of {batch_size} for tenant {tenant_id[:8]} failed "
            f"(rows {','.join(str(r.id)[:8] for r in batch)}, "
            f"attempts now {batch[0].delivery_attempts}-{batch[-1].delivery_attempts}/{_MAX_DELIVERY_ATTEMPTS})"
        )

    return {
        "delivered": delivered,
        "failed": failed,
        "dropped": 0,
        "skipped_in_flight": 0,
        "batch_size": batch_size,
    }


def _reschedule_drain(
    tenant: Tenant,
    channel: str,
    channel_user_id: str,
    *,
    delay_seconds: int = 0,
) -> None:
    """Schedule another drain pass for the same key.

    Called when (a) we just delivered a row and more remain, (b) we
    just dropped a maxed-out row at the head of the queue and want to
    immediately try the next one, or (c) we just woke a hibernated
    container and want to retry once it has booted (``delay_seconds``).
    """
    try:
        from apps.cron.publish import publish_task

        publish_task(
            "drain_pending_messages_for_tenant",
            str(tenant.id),
            channel,
            channel_user_id or "",
            delay_seconds=delay_seconds or None,
            retries=_DRAIN_PUBLISH_RETRIES,
        )
    except Exception:
        logger.exception(
            "drain_pending: failed to re-schedule drain for tenant %s key=%s/%s",
            str(tenant.id)[:8],
            channel,
            (channel_user_id or "")[:24],
        )


def _mark_ios_waking(channel: str, batch: list[PendingMessage]) -> None:
    """Surface a hibernation wake to polling rich clients: stamp
    ``waking_at`` on the batch's AppChatMessage rows so
    ``GET /chat/messages/<id>/`` can render "your assistant is waking up"
    instead of indefinite typing dots. Telegram gets the same signal via
    ``_notify_waking``'s push ack; rich clients have no push transport.
    Idempotent — re-stamping on each boot-grace retry is harmless."""
    if channel != PendingMessage.Channel.IOS or not batch:
        return
    client_ids = _ios_client_msg_ids(batch)
    if not client_ids:
        return
    try:
        from apps.router.models import AppChatMessage

        AppChatMessage.objects.filter(
            tenant_id=batch[0].tenant_id,
            client_msg_id__in=client_ids,
            status=AppChatMessage.Status.PENDING,
        ).update(waking_at=timezone.now())
    except Exception:
        logger.exception("drain_pending: failed to stamp waking_at for ios batch")


def _is_container_down_error(exc: Exception) -> bool:
    """True if ``exc`` from a drain POST means the OpenClaw container isn't
    serving. A hibernated tenant's revision is deactivated, so the Container
    Apps ingress returns 404; a connection-level failure means no replica is
    accepting traffic yet. Both are "wake the container" signals rather than
    "the request is broken" signals.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 404
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


def _notify_waking(tenant: Tenant, channel: str, channel_user_id: str) -> None:
    """Best-effort "waking up, hold on" ack while a hibernated container
    boots — parity with the webhook path's ACK_FRESH response. Without it
    the user faces ~60s of silence after their wake message and assumes it
    failed. Telegram only: LINE hibernated messages are acked by
    line_webhook's own ``handle_hibernated_message`` and only reach the
    drain in the rare enqueue-then-hibernate race.
    """
    if channel != PendingMessage.Channel.TELEGRAM or not channel_user_id:
        return
    try:
        chat_id = int(channel_user_id)
    except (TypeError, ValueError):
        return
    base = _telegram_api_base()
    if not base:
        return
    from apps.router.error_messages import error_msg

    lang = getattr(tenant.user, "language", None) or "en"
    try:
        httpx.post(
            f"{base}/sendMessage",
            json={"chat_id": chat_id, "text": error_msg(lang, "hibernation_waking")},
            timeout=10,
        )
    except Exception:
        logger.exception(
            "drain_pending: failed to send waking-up ack to Telegram for tenant %s",
            str(tenant.id)[:8],
        )


# ---------------------------------------------------------------------------
# Apology for messages dropped past the attempts cap
# ---------------------------------------------------------------------------


def _send_apology_for_stale_pending_message(
    tenant: Tenant,
    msg: PendingMessage,
    age_seconds: float,
) -> None:
    """Notify the user we deliberately didn't process a message that sat
    stuck in the queue too long.

    Shape mirrors ``_send_apology_for_dropped_pending_message`` so the
    LINE/Telegram send paths can stay identical, but the copy explains
    delay (not "we tried and failed") and suggests the user resend if
    still relevant. The minutes-since-send is included so the user can
    place which message slipped through.
    """
    from apps.router.error_messages import error_msg

    excerpt = (msg.user_text or "").strip().replace("\n", " ")
    if len(excerpt) > 50:
        excerpt = excerpt[:50] + "…"

    # Human-friendly approximate age, capped to "hours" granularity past
    # one hour so we don't render "423 minutes ago" for a 7-hour stall.
    minutes = max(1, int(age_seconds // 60))
    if minutes < 60:
        age_label = f"{minutes}m"
    else:
        hours = minutes // 60
        age_label = f"~{hours}h"

    lang = getattr(tenant.user, "language", None) or "en"
    if excerpt:
        text = error_msg(lang, "stale_message_with_excerpt", excerpt=excerpt, age=age_label)
    else:
        text = error_msg(lang, "stale_message", age=age_label)

    if msg.channel == PendingMessage.Channel.LINE:
        line_user_id = msg.channel_user_id or getattr(tenant.user, "line_user_id", None)
        if not line_user_id:
            return
        from apps.router.line_webhook import _send_line_text

        try:
            _send_line_text(line_user_id, text)
        except Exception:
            logger.exception(
                "drain_pending: failed to push stale apology to LINE for tenant %s",
                str(tenant.id)[:8],
            )
    elif msg.channel == PendingMessage.Channel.TELEGRAM:
        try:
            chat_id = int(msg.channel_user_id)
        except (TypeError, ValueError):
            logger.warning(
                "drain_pending: cannot send telegram stale apology — invalid chat_id %r",
                msg.channel_user_id,
            )
            return
        base = _telegram_api_base()
        if not base:
            return
        try:
            httpx.post(
                f"{base}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
        except Exception:
            logger.exception(
                "drain_pending: failed to push stale apology to Telegram for tenant %s",
                str(tenant.id)[:8],
            )


def _send_apology_for_dropped_pending_message(tenant: Tenant, msg: PendingMessage) -> None:
    """Notify the user we couldn't process their queued message after the
    attempts cap.

    Mirrors ``_send_apology_for_dropped_message`` in
    ``apps/orchestrator/hibernation.py`` (which handles hibernation
    BufferedMessages). Same translation framework, same channel-native
    plain push semantics — sent OUTSIDE the assistant pipeline so the
    user knows it's a system status, not assistant content.

    Implemented separately rather than reused so a future divergence
    (e.g. different copy for warm-tenant vs cold-start failures) doesn't
    require splitting one helper into two with awkward conditionals.
    """
    from apps.router.error_messages import error_msg, strip_internal_framing

    # Defense in depth: even though every call site is supposed to pass
    # ``raw_user_text`` so PendingMessage.user_text is clean, peel any
    # ``[System: \u2026]`` / ``[Now: \u2026]`` / ``[chat: \u2026]`` / ``[User tapped button: \u2026]``
    # framing off the head before quoting. The user shouldn't see this.
    excerpt = strip_internal_framing(msg.user_text or "").strip().replace("\n", " ")
    if len(excerpt) > 50:
        excerpt = excerpt[:50] + "\u2026"

    lang = getattr(tenant.user, "language", None) or "en"
    if excerpt:
        text = error_msg(lang, "dropped_message_with_excerpt", excerpt=excerpt)
    else:
        text = error_msg(lang, "dropped_message")

    if msg.channel == PendingMessage.Channel.LINE:
        line_user_id = msg.channel_user_id or getattr(tenant.user, "line_user_id", None)
        if not line_user_id:
            return
        from apps.router.line_webhook import _send_line_text

        try:
            _send_line_text(line_user_id, text)
        except Exception:
            logger.exception(
                "drain_pending: failed to push apology to LINE for tenant %s",
                str(tenant.id)[:8],
            )
    elif msg.channel == PendingMessage.Channel.TELEGRAM:
        try:
            chat_id = int(msg.channel_user_id)
        except (TypeError, ValueError):
            logger.warning(
                "drain_pending: cannot send telegram apology — invalid chat_id %r",
                msg.channel_user_id,
            )
            return
        # Plain text via Bot API — no parse_mode so unusual chars don't
        # block delivery of the apology itself.
        base = _telegram_api_base()
        if not base:
            return
        try:
            httpx.post(
                f"{base}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
        except Exception:
            logger.exception(
                "drain_pending: failed to push apology to Telegram for tenant %s",
                str(tenant.id)[:8],
            )


# ---------------------------------------------------------------------------
# Channel-specific drain helpers
# ---------------------------------------------------------------------------


def _build_batch_chat_content(batch: list[PendingMessage], fallback_user_id: str) -> tuple[str, str, str]:
    """Build the ``content`` string + routing context for a deliverable batch.

    Returns ``(content, user_param, user_timezone)``.

    Singleton batches (``len(batch) == 1``) preserve the existing per-row
    on-the-wire shape: the row's pre-decorated ``payload.message_text``
    flows straight through, with markers as baked in at enqueue time.

    Coalesced batches (``len(batch) > 1``) build a fresh prompt at drain
    time using ``format_coalesced_user_content``: the datetime + coalesced
    chat marker are emitted ONCE (from the latest row's routing context),
    then each row's raw ``user_text`` is appended with an index +
    timestamp. The intent is the agent reads N delineated follow-ups
    instead of N separate per-turn replies.
    """
    from apps.router.services import format_coalesced_user_content

    if len(batch) == 1:
        msg = batch[0]
        payload = msg.payload or {}
        content = payload.get("message_text") or ""
        user_param = payload.get("user_param") or msg.channel_user_id or fallback_user_id
        user_tz = payload.get("user_timezone") or "UTC"
        return content, user_param, user_tz

    # Coalesced: build markers fresh from the latest row's context so the
    # datetime marker reflects "now", not whatever was true at enqueue
    # time for row #1 (which could be many seconds older during a real
    # cold-start burst).
    latest = batch[-1]
    latest_payload = latest.payload or {}
    user_param = latest_payload.get("user_param") or latest.channel_user_id or fallback_user_id
    user_tz = latest_payload.get("user_timezone") or "UTC"

    raw_texts = [(row.user_text or "") for row in batch]
    timestamps = [row.created_at for row in batch]
    content = format_coalesced_user_content(
        raw_texts,
        user_timezone=user_tz,
        timestamps=timestamps,
    )
    return content, user_param, user_tz


def _drain_line_batch(tenant: Tenant, batch: list[PendingMessage], timeout: float) -> bool:
    """Forward a deliverable LINE batch to the container as one OC turn.

    ``len(batch) == 1`` preserves the historical per-row shape verbatim
    (same on-the-wire payload, same single ``record_usage`` call with
    ``message_count=1``). ``len(batch) > 1`` is the cold-start coalesce
    path: one POST, one relay, one ``record_usage`` with
    ``message_count=len(batch)`` so per-tenant message counters track
    user-perceived sends, not LLM turns.

    The batch claim guarantees all rows share ``channel_user_id`` and
    none are voice rows once ``len(batch) > 1`` — voice always stays a
    singleton (see ``_claim_pending_batch_for_key``).

    Returns ``True`` when the gateway returned a healthy (non-credit-limit)
    response — proof the container is awake. Returns ``False`` for the
    empty-batch and OpenRouter credit-limit early-returns (the latter
    *intentionally* hibernates the tenant, so the caller must NOT treat it
    as a liveness signal). See the stale-hibernation reconcile in
    ``drain_pending_messages_for_tenant_task``.
    """
    if not batch:
        return False

    from apps.router.line_webhook import relay_ai_response_to_line

    line_user_id = batch[0].channel_user_id
    content, user_param, user_tz = _build_batch_chat_content(batch, line_user_id)
    # ``reply_token`` is intentionally NOT used: by the time the queue
    # drains, the LINE Reply API window (~1 min) is almost always
    # closed. We always Push.

    url = f"https://{tenant.container_fqdn}/v1/chat/completions"
    from apps.cron.gateway_client import get_gateway_token_for_tenant

    gateway_token = get_gateway_token_for_tenant(tenant)

    chat_payload = {
        # OpenClaw 5.7's /v1/chat/completions handler hard-rejects any
        # body ``model`` value that isn't ``openclaw``, ``openclaw/default``,
        # ``openclaw:<id>``, or ``agent:<id>`` (returns 400). The real
        # upstream model is selected inside the runtime; attribution is
        # done client-side in ``_record_usage_safe`` via
        # ``resolve_model_for_attribution`` (response → tenant primary
        # fallback). See PR following #720 for the regression context.
        "model": "openclaw",
        "messages": [{"role": "user", "content": content}],
        "user": user_param,
    }
    headers = {
        "Authorization": f"Bearer {gateway_token}",
        "X-User-Timezone": user_tz,
        "X-Line-User-Id": line_user_id,
        "X-Channel": "line",
    }

    resp = httpx.post(url, json=chat_payload, headers=headers, timeout=timeout)
    if _looks_like_openrouter_credit_limit(resp):
        _handle_openrouter_credit_limit(tenant, channel="line", channel_user_id=line_user_id)
        return False
    resp.raise_for_status()
    result = resp.json()

    ai_text = _extract_ai_response(result)
    if ai_text and line_user_id:
        try:
            relay_ai_response_to_line(tenant, line_user_id, ai_text)
        except Exception:
            logger.exception(
                "drain_pending: failed to relay LINE response for tenant %s",
                str(tenant.id)[:8],
            )

    _capture_conversation_turn(tenant, "line", line_user_id, batch, ai_text)
    _record_usage_safe(tenant, result, message_count=len(batch))
    return True


def _capture_conversation_turn(
    tenant: Tenant,
    channel: str,
    channel_user_id: str,
    batch: list[PendingMessage],
    ai_text: str | None,
) -> None:
    """Persist this drain's conversation turn for the USER.md "Conversation so
    far" digest. Fail-open — capture must never affect message delivery.

    iOS is intentionally NOT captured here: it's already durable in
    ``AppChatMessage`` and the digest reads that table directly (no double-store).
    """
    try:
        from apps.router.conversation_capture import (
            clean_reply_for_capture,
            join_user_texts,
            record_conversation_turn,
        )

        record_conversation_turn(
            tenant=tenant,
            channel=channel,
            channel_user_id=channel_user_id or "",
            user_text=join_user_texts(batch),
            reply_text=clean_reply_for_capture(tenant, ai_text),
        )
    except Exception:
        logger.exception("drain_pending: conversation capture failed (non-fatal)")


def _drain_telegram_batch(tenant: Tenant, batch: list[PendingMessage], timeout: float) -> bool:
    """Forward a deliverable Telegram batch to the container as one OC turn.

    Singleton vs coalesced behaviour mirrors ``_drain_line_batch``; see
    that docstring. The batch claim guarantees all rows share
    ``channel_user_id`` (so all rows belong to the same Telegram chat).

    Returns ``True`` on a healthy (non-credit-limit) gateway response — see
    ``_drain_line_batch`` for the liveness-signal contract.
    """
    if not batch:
        return False

    chat_id_str = batch[0].channel_user_id
    try:
        chat_id = int(chat_id_str)
    except (TypeError, ValueError):
        raise ValueError(f"telegram drain: invalid chat_id {chat_id_str!r}")

    content, user_param, user_tz = _build_batch_chat_content(batch, chat_id_str)

    url = f"https://{tenant.container_fqdn}/v1/chat/completions"
    from apps.cron.gateway_client import get_gateway_token_for_tenant

    gateway_token = get_gateway_token_for_tenant(tenant)

    chat_payload = {
        # See ``_drain_line_batch`` for why this stays the ``openclaw``
        # sentinel rather than a resolved tenant primary.
        "model": "openclaw",
        "messages": [{"role": "user", "content": content}],
        "user": user_param,
    }
    headers = {
        "Authorization": f"Bearer {gateway_token}",
        "X-User-Timezone": user_tz,
        "X-Telegram-Chat-Id": str(chat_id),
        "X-Channel": "telegram",
    }

    # Send a typing pulse before the slow POST so the user sees activity.
    _send_telegram_typing_safe(chat_id)

    resp = httpx.post(url, json=chat_payload, headers=headers, timeout=timeout)
    if _looks_like_openrouter_credit_limit(resp):
        _handle_openrouter_credit_limit(tenant, channel="telegram", channel_user_id=str(chat_id))
        return False
    resp.raise_for_status()
    result = resp.json()

    ai_text = _extract_ai_response(result)
    if ai_text:
        relay_ai_response_to_telegram(tenant, chat_id, ai_text)

    _capture_conversation_turn(tenant, "telegram", chat_id_str, batch, ai_text)
    _record_usage_safe(tenant, result, message_count=len(batch))
    return True


# ---------------------------------------------------------------------------
# iOS / rich-client (app) drain — persists the reply for the client to poll
# instead of relaying to a push channel API. Routes through the tenant
# runtime like Telegram/LINE (same USER.md/memory); the ``user`` param is
# ``thread:<id>`` so each ChatThread is its own OpenClaw session.
# ---------------------------------------------------------------------------


def _drain_ios_batch(tenant: Tenant, batch: list[PendingMessage], timeout: float) -> bool:
    """Forward a deliverable iOS/app batch to the container as one OC turn,
    then PERSIST the reply to ``AppChatMessage`` for the client to poll.

    Mirrors ``_drain_telegram_batch`` on the wire, but instead of relaying
    to a channel push API it stores the reply keyed by the client-supplied
    ``client_msg_id`` (carried on each row's payload). Returns ``True`` on a
    healthy gateway response (liveness signal — see ``_drain_line_batch``).
    On an OpenRouter credit-limit the turn(s) are marked errored so polling
    clients aren't stuck pending.
    """
    if not batch:
        return False

    thread_id = batch[0].channel_user_id
    content, user_param, user_tz = _build_batch_chat_content(batch, thread_id)

    url = f"https://{tenant.container_fqdn}/v1/chat/completions"
    from apps.cron.gateway_client import get_gateway_token_for_tenant

    gateway_token = get_gateway_token_for_tenant(tenant)

    chat_payload = {
        # See ``_drain_line_batch`` for why this stays the ``openclaw``
        # sentinel rather than a resolved tenant primary.
        "model": "openclaw",
        "messages": [{"role": "user", "content": content}],
        "user": user_param,
    }
    headers = {
        "Authorization": f"Bearer {gateway_token}",
        "X-User-Timezone": user_tz,
        "X-Channel": "ios",
    }

    resp = httpx.post(url, json=chat_payload, headers=headers, timeout=timeout)
    if _looks_like_openrouter_credit_limit(resp):
        _handle_openrouter_credit_limit(tenant, channel="ios", channel_user_id=thread_id)
        _store_ios_turn_error(tenant, batch, "budget_exhausted")
        return False
    resp.raise_for_status()
    result = resp.json()

    ai_text = _extract_ai_response(result)
    _store_ios_turn_reply(tenant, batch, ai_text)
    _record_usage_safe(tenant, result, message_count=len(batch))
    return True


def _ios_client_msg_ids(batch: list[PendingMessage]) -> list[str]:
    ids = [(row.payload or {}).get("client_msg_id") for row in batch]
    return [cid for cid in ids if cid]


def _store_ios_turn_reply(tenant: Tenant, batch: list[PendingMessage], ai_text: str | None) -> None:
    """Persist the assistant reply onto the AppChatMessage rows for this
    batch so the polling client can read it. Empty / gateway-error replies
    flip the turn to ``error`` so the client doesn't poll forever."""
    from apps.router.models import AppChatMessage

    client_ids = _ios_client_msg_ids(batch)
    if not client_ids:
        return
    now = timezone.now()
    if ai_text:
        text = _clean_assistant_text_for_app(tenant, ai_text)
        AppChatMessage.objects.filter(tenant=tenant, client_msg_id__in=client_ids).update(
            reply_text=text,
            status=AppChatMessage.Status.READY,
            replied_at=now,
        )
    else:
        AppChatMessage.objects.filter(tenant=tenant, client_msg_id__in=client_ids).update(
            status=AppChatMessage.Status.ERROR,
            error="empty_response",
            replied_at=now,
        )


def _store_ios_turn_error(tenant: Tenant, batch: list[PendingMessage], reason: str) -> None:
    from apps.router.models import AppChatMessage

    client_ids = _ios_client_msg_ids(batch)
    if not client_ids:
        return
    AppChatMessage.objects.filter(tenant=tenant, client_msg_id__in=client_ids).update(
        status=AppChatMessage.Status.ERROR,
        error=reason,
        replied_at=timezone.now(),
    )


def _clean_assistant_text_for_app(tenant: Tenant, ai_text: str) -> str:
    """Rehydrate PII, record + strip ``[[insight:]]`` markers, and strip
    ``[[chart:]]`` / ``MEDIA:`` markers (the app can't render workspace
    file paths) so the stored reply is clean display text. Mirrors the
    relevant parts of ``relay_ai_response_to_telegram``."""
    entity_map = getattr(tenant, "pii_entity_map", None)
    if entity_map:
        try:
            from apps.pii.redactor import rehydrate_text

            ai_text = rehydrate_text(ai_text, entity_map)
        except Exception:
            logger.exception("drain_pending: PII rehydrate failed (ios)")

    try:
        from apps.insights.markers import extract_and_record_insights

        ai_text = extract_and_record_insights(ai_text, tenant=tenant)
    except Exception:
        logger.exception("insight marker extraction failed (ios drain)")

    ai_text = re.sub(r"\[\[chart:\w+(?:\|.+?)?\]\]", "", ai_text)
    ai_text = re.sub(r"MEDIA:\S+", "", ai_text)
    return ai_text.strip()


# ---------------------------------------------------------------------------
# Telegram response relay (mirrors ``relay_ai_response_to_line`` in
# line_webhook.py — kept here so the queue can deliver Telegram replies
# from the Django web worker without having to reach into the long-lived
# poller process).
# ---------------------------------------------------------------------------


def relay_ai_response_to_telegram(tenant: Tenant, chat_id: int, ai_text: str) -> bool:
    """Format and deliver an AI assistant response to Telegram.

    Mirrors ``relay_ai_response_to_line``. Used by the queue drain task
    so we don't need a back-channel to the long-lived poller process.
    The poller itself still calls its own ``_send_rich_response`` for
    the synchronous live path it owns; this helper is for the queue.

    Handles:
      - PII rehydration
      - ``[[chart:type|params]]`` chart rendering + image upload
      - ``MEDIA:path`` image references (via ``_send_telegram_photo``)
      - Markdown chunking + Telegram parse_mode fallback to plain text
    """
    if not ai_text or not chat_id:
        return False

    # Rehydrate PII placeholders before sending to user.
    entity_map = getattr(tenant, "pii_entity_map", None)
    if entity_map:
        try:
            from apps.pii.redactor import rehydrate_text

            ai_text = rehydrate_text(ai_text, entity_map)
        except Exception:
            logger.exception("drain_pending: PII rehydrate failed")

    text = ai_text

    # Log-only instrumentation: ASCII chart leakage when no marker emitted.
    from apps.router.output_guards import log_ascii_chart_leak

    log_ascii_chart_leak(text, tenant_id=tenant.id, channel="telegram_drain")

    # Extract [[insight:slug]]statement[[/insight]] markers, write
    # AssistantInsight rows, strip marker tokens. Runs before chart
    # processing so insights nested near chart markers still record.
    try:
        from apps.insights.markers import extract_and_record_insights

        text = extract_and_record_insights(text, tenant=tenant)
    except Exception:
        logger.exception("insight marker extraction failed (telegram drain)")

    # Render [[chart:type]] markers into images and inject MEDIA: paths
    # (same convention as poller._send_rich_response so the agent
    # doesn't have to know which path it's on).
    chart_pattern = re.compile(r"\[\[chart:(\w+)(?:\|(.+?))?\]\]")
    for match in chart_pattern.finditer(text):
        chart_type = match.group(1)
        raw_params = match.group(2) or ""
        params = dict(p.split("=", 1) for p in raw_params.split(",") if "=" in p)
        try:
            from apps.router.charts import render_chart

            png_bytes = render_chart(chart_type, tenant, params)
            if png_bytes:
                import uuid as _uuid

                fname = f"charts/{chart_type}_{_uuid.uuid4().hex[:8]}.png"
                fpath = f"workspace/{fname}"
                from apps.orchestrator.azure_client import upload_workspace_file_binary

                upload_workspace_file_binary(str(tenant.id), fpath, png_bytes)
                container_path = f"/home/node/.openclaw/workspace/{fname}"
                text = text.replace(match.group(0), f"MEDIA:{container_path}")
            else:
                text = text.replace(match.group(0), "")
        except Exception:
            logger.exception("Chart rendering failed for %s (telegram drain)", chart_type)
            text = text.replace(match.group(0), "")

    # Strip MEDIA references and attempt to send any embedded images
    # via sendPhoto. We don't bring the full _send_rich_response logic
    # here (no inline button parsing yet) — that's a follow-up if it
    # turns out queued Telegram replies need it. Hibernation-buffered
    # delivery for Telegram has the same limitation today.
    media_pattern = re.compile(
        r"MEDIA:(\S+\.(?:jpg|jpeg|png|gif|webp))",
        re.IGNORECASE,
    )
    workspace_pattern = re.compile(
        r"(/home/node/\.openclaw/workspace/\S+\.(?:jpg|jpeg|png|gif|webp))",
        re.IGNORECASE,
    )

    for path in media_pattern.findall(text) + workspace_pattern.findall(text):
        if path.startswith("./"):
            path = f"/home/node/.openclaw/workspace/{path[2:]}"
        if path.startswith("/home/node/"):
            try:
                _send_telegram_photo(chat_id, path, tenant)
            except Exception:
                logger.exception("drain_pending: telegram photo send failed (%s)", path)

    text = media_pattern.sub("", text)
    text = workspace_pattern.sub("", text).strip()

    if not text:
        return True

    return _send_telegram_markdown(chat_id, text)


# ---------------------------------------------------------------------------
# Telegram low-level helpers (parallel to TelegramPoller._send_*)
# ---------------------------------------------------------------------------


def _telegram_api_base() -> str | None:
    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        return None
    return f"{_TELEGRAM_API_BASE}{bot_token}"


def _send_telegram_typing_safe(chat_id: int) -> None:
    """Best-effort typing indicator. Never raises."""
    base = _telegram_api_base()
    if not base:
        return
    try:
        httpx.post(
            f"{base}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5,
        )
    except Exception:  # nosec — typing is non-critical
        logger.debug("drain_pending: telegram typing failed", exc_info=True)


_TG_MAX_LEN = 4096


def _split_telegram_message(text: str, max_len: int = _TG_MAX_LEN) -> list[str]:
    """Split a long Telegram message on paragraph/line/word boundaries.

    Mirrors ``TelegramPoller._split_message``.
    """
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, max_len)
        if cut == -1:
            cut = remaining.rfind("\n", 0, max_len)
        if cut == -1:
            cut = remaining.rfind(" ", 0, max_len)
        if cut == -1:
            cut = max_len
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return [c for c in chunks if c]


def _send_telegram_markdown(chat_id: int, text: str) -> bool:
    """Render the assistant's markdown to Telegram HTML and deliver it.

    The agent emits CommonMark/GFM; Telegram's legacy ``Markdown`` parse-mode
    leaks ``##`` headings, ``---`` rules, ``**bold**`` and tables literally, so
    we render to Telegram's HTML subset (``apps.router.telegram_format``) —
    bold headings, aligned monospace tables, real anchors, no visible markdown.
    Each block-bounded chunk is sent with ``parse_mode="HTML"``; on the rare
    rejection we degrade that chunk to tag-free text (still markdown-free).
    """
    base = _telegram_api_base()
    if not base:
        logger.warning("drain_pending: cannot send telegram message — no bot token")
        return False

    from apps.router.telegram_format import render_telegram_html, strip_telegram_html

    chunks = render_telegram_html(text)
    if not chunks:
        return True

    overall = True
    for i, chunk in enumerate(chunks):
        if i > 0:
            time.sleep(0.3)  # brief delay between chunks (matches poller)
        try:
            resp = httpx.post(
                f"{base}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.is_success:
                continue
            if resp.status_code == 400:
                # HTML rejected — retry as tag-free plain text (no markdown).
                plain = httpx.post(
                    f"{base}/sendMessage",
                    json={"chat_id": chat_id, "text": strip_telegram_html(chunk)},
                    timeout=10,
                )
                overall = overall and plain.is_success
                continue
            logger.warning(
                "drain_pending: sendMessage failed (%s): %s",
                resp.status_code,
                resp.text[:200],
            )
            overall = False
        except Exception:
            logger.exception("drain_pending: sendMessage exception")
            overall = False

    return overall


def _send_telegram_photo(chat_id: int, photo_path: str, tenant: Tenant) -> bool:
    """Download a photo from the tenant's workspace file share and send
    it to the Telegram chat.

    Mirrors ``TelegramPoller._send_photo`` — kept here so the drain task
    doesn't need to reach into the poller process.
    """
    base = _telegram_api_base()
    if not base:
        return False

    try:
        from apps.orchestrator.azure_client import _is_mock

        if _is_mock():
            return False

        share_path = photo_path
        if "/workspace/" in share_path:
            share_path = "workspace/" + share_path.split("/workspace/", 1)[1]

        account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
        if not account_name:
            return False

        from azure.storage.fileshare import ShareFileClient

        from apps.orchestrator.azure_client import get_storage_client

        storage_client = get_storage_client()
        keys = storage_client.storage_accounts.list_keys(
            settings.AZURE_RESOURCE_GROUP,
            account_name,
        )
        account_key = keys.keys[0].value
        share_name = f"ws-{str(tenant.id)[:20]}"

        file_client = ShareFileClient(
            account_url=f"https://{account_name}.file.core.windows.net",
            share_name=share_name,
            file_path=share_path,
            credential=account_key,
        )
        data = file_client.download_file().readall()

        ext = share_path.rsplit(".", 1)[-1].lower() if "." in share_path else "jpg"
        mime = {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
        files = {"photo": (f"image.{ext}", data, mime)}
        form_data = {"chat_id": str(chat_id)}

        resp = httpx.post(
            f"{base}/sendPhoto",
            data=form_data,
            files=files,
            timeout=15,
        )
        if resp.is_success:
            return True
        logger.warning("drain_pending: sendPhoto failed (%s): %s", resp.status_code, resp.text[:200])
        return False
    except Exception:
        logger.exception("drain_pending: sendPhoto exception (%s)", photo_path)
        return False


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _extract_ai_response(result: Any) -> str | None:
    """Pull the assistant text out of a chat-completions response, or
    None if the response is empty / a known gateway-error string."""
    if not isinstance(result, dict):
        return None
    try:
        choices = result.get("choices", [])
        if not choices:
            return None
        text = choices[0].get("message", {}).get("content")
        if text and text.strip() not in _GATEWAY_ERROR_STRINGS:
            return text
    except (IndexError, KeyError, TypeError):
        return None
    return None


def _record_usage_safe(tenant: Tenant, result: Any, *, message_count: int = 1) -> None:
    """Record token usage from a chat-completions response. Swallows
    errors so a billing failure can never wedge the queue.

    ``message_count`` defaults to 1; the cold-start coalesce path passes
    ``len(batch)`` so per-tenant ``messages_today`` / ``messages_this_month``
    track user-perceived sends instead of LLM turns. Tokens + cost reflect
    actual inference work and are NOT scaled.
    """
    if not isinstance(result, dict):
        return
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return

    input_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
    model_used = resolve_model_for_attribution(tenant, result)

    if not (input_tokens or output_tokens):
        return

    try:
        record_usage(
            tenant=tenant,
            event_type="message",
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            model_used=model_used,
            message_count=max(1, int(message_count)),
        )
    except Exception:
        logger.exception("drain_pending: failed to record usage for tenant %s", tenant.id)


# ---------------------------------------------------------------------------
# Reaper — picks up rows whose original drain task never ran
# ---------------------------------------------------------------------------


def reap_stuck_inbound_messages_task() -> dict:
    """Republish drain tasks for pending rows whose original drain
    never ran (or ran and exited without state transition).

    Why this exists
    ---------------

    ``enqueue_message_for_tenant`` publishes a per-row drain task to
    QStash. Three failure modes can leave a row stuck in ``PENDING``
    with no follow-up drain firing:

      1. ``publish_task`` itself raised (network blip, QStash 5xx,
         token rotation). The caller catches + swallows so the inbound
         webhook still ACKs LINE/Telegram fast. No QStash entry exists,
         so no retry ever fires.
      2. ``publish_task`` succeeded but QStash's HTTP delivery to Django
         hit a 5xx for all ``_DRAIN_PUBLISH_RETRIES`` attempts (e.g. OC
         container down for >5 min). The message lands in DLQ and
         nothing in this codebase reads the DLQ.
      3. A drain task claimed the row (lease taken) but the gunicorn
         worker died before the row's state transitioned past ``PENDING``
         (OOM-kill, deploy mid-flight, 300s worker timeout). The lease
         eventually expires but no event re-publishes the drain.

    In all three cases the row sits ``PENDING`` until the user's NEXT
    inbound arrives, at which point a fresh drain task drains the
    backlog FIFO — producing "responding to questions from hours ago"
    UX (the canary screenshot incident, 2026-05-23). This task closes
    the gap: every minute it scans for stuck rows and republishes a
    drain per ``(tenant, channel, channel_user_id)`` key. The drain's
    ``SKIP-LOCKED`` claim handles concurrency cleanly even if the
    original drain happens to fire at the same moment.

    The reaper does NOT process rows itself — it only republishes drain
    tasks. The drain task remains the single point where chat
    completions get POSTed at OC, so its serialization guarantees
    (one POST at a time per session, attempt cap, stale-age guard) hold
    regardless of who scheduled the drain. The drain's stale-age guard
    is what prevents the reaper from delivering 7-hour-old messages to
    OC — it'll mark them ``failed`` with an apology instead.
    """
    from apps.cron.publish import publish_task

    now = timezone.now()
    cutoff = now - timedelta(seconds=_REAPER_STUCK_AGE_SECONDS)

    # Distinct keys with stuck rows. Row-level locks aren't needed here —
    # the per-row claim inside ``drain_pending_messages_for_tenant_task``
    # provides serialization; the reaper just identifies which queues to
    # kick. We sort by key for deterministic ordering across reaper
    # ticks so test fixtures are easy to write.
    stuck_keys = (
        PendingMessage.objects.filter(
            delivery_status=PendingMessage.Status.PENDING,
            created_at__lt=cutoff,
        )
        .filter(models.Q(delivery_in_flight_until__isnull=True) | models.Q(delivery_in_flight_until__lt=now))
        .values_list("tenant_id", "channel", "channel_user_id")
        .distinct()
        .order_by("tenant_id", "channel", "channel_user_id")[:_REAPER_BATCH_LIMIT]
    )

    keys = list(stuck_keys)
    republished = 0
    errors = 0
    for tenant_id, channel, channel_user_id in keys:
        try:
            publish_task(
                "drain_pending_messages_for_tenant",
                str(tenant_id),
                channel,
                channel_user_id or "",
                retries=_DRAIN_PUBLISH_RETRIES,
            )
            republished += 1
        except Exception:
            logger.exception(
                "reap_stuck_inbound: failed to republish drain for tenant %s key=%s/%s",
                str(tenant_id)[:8],
                channel,
                (channel_user_id or "")[:24],
            )
            errors += 1

    # Only log when we actually did something — steady-state ticks
    # (no stuck rows) should be silent so the platform_logs feed isn't
    # buried in zero-op heartbeats.
    if keys:
        logger.warning(
            "reap_stuck_inbound: %d stuck key(s), %d republished, %d errors",
            len(keys),
            republished,
            errors,
        )

    return {
        "stuck_keys": len(keys),
        "republished": republished,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Misc — kept so callers can import this module without importing every
# helper individually.
# ---------------------------------------------------------------------------

# Some surface used by tests / callers — keep imports stable.
__all__ = [
    "PendingMessage",
    "drain_pending_messages_for_tenant_task",
    "enqueue_message_for_tenant",
    "reap_stuck_inbound_messages_task",
    "relay_ai_response_to_telegram",
]
