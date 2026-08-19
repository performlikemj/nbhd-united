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
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any

import httpx
from django.conf import settings
from django.db import models, transaction
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone

from apps.billing.services import (
    record_usage,
    resolve_model_for_attribution,
)
from apps.common.eval_sink import blocks_real_transport_for_identifier, suppresses_real_transport
from apps.pii.authoring import placeholder_redactions
from apps.router.models import AppChatMessage, ChatThread, PendingMessage, RuntimeWriteActivity
from apps.router.reply_text import clamp_reply_text
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

# A terminal app drop gets one delayed replay only. QStash delivery retries are
# disabled for the replay task itself; once it runs, the shared PendingMessage
# queue owns the re-submitted inbound under its existing bounded semantics.
_DROPPED_RETRY_DELAY_SECONDS = 60
_DROPPED_RETRY_HEALTH_DELAY_SECONDS = 120

# Covers the observed container-kill-at-turn-start class (for example, the
# 19:40:52→19:41:09 incident). A completed model round-trip plus raw cron.add
# inside 30 seconds is implausible; the residual double-cron risk in this bound
# is accepted until the deferred wrapping-plugin guard lands.
_DROPPED_RETRY_MAX_DROP_AGE_SECONDS = 30

# Narration is only corroborating evidence: exact ``thinking`` is required,
# while the control plane's runtime-write record is authoritative for mutations.
_DROPPED_RETRY_SAFE_PHASE = "thinking"

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

# A live in-flight lease is only safe to honor while the tenant container is
# actually serving. Keep this probe short: it runs only when a drain would
# otherwise defer behind a live lease, or after a transport-family POST
# failure where container liveness decides between wake/defer and the normal
# bounded failure path.
_CONTAINER_HEALTH_TIMEOUT_SECONDS = 3.0

# Reaper sweep. Pending rows older than this with no live in-flight lease
# are presumed stuck (publish_task raised + got swallowed, or QStash
# delivered the drain task into the Django 5xx → DLQ pit, or a worker
# died mid-claim). The reaper republishes a fresh drain task per key —
# the drain's SKIP-LOCKED claim handles concurrency cleanly.
_REAPER_STUCK_AGE_SECONDS = 90

# A brand-new tenant's container is built asynchronously (~1 min). While it
# is still provisioning it has no FQDN yet — but it IS coming online, so a
# message that arrives in that window is buffered + re-driven (not failed)
# until the container lands. This caps how long we keep deferring before
# giving up, so a genuinely stuck/failed provision can't loop forever.
_PROVISION_MAX_WAIT_SECONDS = 300

# Cap per reaper tick so a pathological backlog can't blow up the cron
# worker budget. At 60s cadence + 200 keys/tick the steady-state ceiling
# is ~3.3 republished drains/second across the entire fleet, which is
# well under QStash's free-tier rate limit.
_REAPER_BATCH_LIMIT = 200

# Defense-in-depth for the creation-time gap between AppChatMessage(PENDING)
# and its PendingMessage queue row. A worker death in that gap leaves no queue
# row for the normal reaper to find, so a separate bounded sweep terminalizes
# tenant-runtime turns that have remained orphaned for 20 minutes.
_STALE_APP_CHAT_AGE_SECONDS = 20 * 60
_STALE_APP_CHAT_BATCH_LIMIT = 200

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


class DeliveryState(str, Enum):  # noqa: UP042 - ratified public contract
    SENT = "sent"
    PARTIAL = "partial"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class SendResult:
    state: DeliveryState
    delivered_chunks: int = 0
    total_chunks: int = 0
    detail: str = ""

    def __bool__(self) -> bool:
        return self.state in (DeliveryState.SENT, DeliveryState.PARTIAL)


class Disposition(str, Enum):  # noqa: UP042 - ratified public contract
    DELIVER = "deliver"
    RETRY = "retry"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class DrainOutcome:
    disposition: Disposition
    delivery: DeliveryState
    gateway_responded: bool
    reason: str = ""
    delivered_chunks: int = 0
    total_chunks: int = 0
    assistant_text: str = ""
    model_response_ref: str = ""

    def __bool__(self) -> bool:
        return self.gateway_responded


def _drain_outcome_from_send_result(
    result: SendResult,
    *,
    assistant_text: str = "",
    model_response_ref: str = "",
) -> DrainOutcome:
    disposition = Disposition.RETRY if result.state is DeliveryState.FAILED else Disposition.DELIVER
    return DrainOutcome(
        disposition=disposition,
        delivery=result.state,
        gateway_responded=True,
        reason=result.detail or ("relay_failed" if result.state is DeliveryState.FAILED else ""),
        delivered_chunks=result.delivered_chunks,
        total_chunks=result.total_chunks,
        assistant_text=assistant_text,
        model_response_ref=model_response_ref,
    )


@dataclass(frozen=True)
class _PreparedPendingUserTranscript:
    source_event_id: str
    occurred_at: Any
    redaction: Any | None = None
    quarantine: Any | None = None


@dataclass(frozen=True)
class _PreparedPendingTranscript:
    source_type: str
    channel: str
    turn_id: Any
    thread_key: str
    primary_source_event_id: str
    users: tuple[_PreparedPendingUserTranscript, ...]
    assistant_redaction: Any | None = None
    assistant_quarantine: Any | None = None
    assistant_occurred_at: Any | None = None
    delivery_state: str = ""
    delivered_chunks: int = 0
    total_chunks: int = 0
    model_response_ref: str = ""


def _pending_transcript_source(channel: str) -> tuple[str, str]:
    from apps.transcripts.models import TranscriptEvent

    if channel == PendingMessage.Channel.IOS:
        return TranscriptEvent.SourceType.IOS_QUEUED, "client_msg_id"
    if channel == PendingMessage.Channel.TELEGRAM:
        return TranscriptEvent.SourceType.TELEGRAM_POLLER, "provider_event_id"
    if channel == PendingMessage.Channel.LINE:
        return TranscriptEvent.SourceType.LINE, "webhook_event_id"
    raise ValueError(f"Unknown transcript channel: {channel!r}")


def _prepare_pending_transcript(
    tenant: Tenant,
    batch: list[PendingMessage],
    outcome: DrainOutcome,
    *,
    include_assistant: bool,
) -> _PreparedPendingTranscript | None:
    """Confirm and encrypt a queue turn before its durable write transaction."""
    if not getattr(tenant, "recall_capture_enabled", False) or not batch:
        return None

    from apps.transcripts.capture import derive_turn_id, encrypt_transcript_text

    source_type, payload_id_key = _pending_transcript_source(batch[0].channel)
    ordered = sorted(batch, key=lambda row: (row.created_at, str(row.id)))
    source_ids = [
        str((row.payload.get(payload_id_key) if isinstance(row.payload, dict) else None) or row.id) for row in ordered
    ]
    turn_id = derive_turn_id(tenant.id, source_type, source_ids[0])
    channel = batch[0].channel
    thread_key = batch[0].channel_user_id if channel == PendingMessage.Channel.IOS else channel
    assistant_occurred_at = timezone.now() if include_assistant and outcome.assistant_text else None

    try:
        from apps.pii.redactor import (
            RedactionOutcome,
            confirm_assistant_output,
            confirmed_from_receipt_row,
            redaction_receipt,
        )

        users = []
        for row, source_event_id in zip(ordered, source_ids, strict=True):
            confirmed = confirmed_from_receipt_row(row.payload, row.user_text or "")
            receipt = redaction_receipt(row.payload) if confirmed is None else None
            users.append(
                _PreparedPendingUserTranscript(
                    source_event_id=source_event_id,
                    occurred_at=row.created_at,
                    redaction=(encrypt_transcript_text(tenant, confirmed) if confirmed is not None else None),
                    quarantine=receipt if confirmed is None else None,
                )
            )

        assistant_redaction = None
        assistant_quarantine = None
        if include_assistant and outcome.assistant_text:
            confirmed_assistant = confirm_assistant_output(tenant, outcome.assistant_text)
            if confirmed_assistant is not None:
                assistant_redaction = encrypt_transcript_text(tenant, confirmed_assistant)
            else:
                assistant_quarantine = RedactionOutcome(
                    text="",
                    confirmed=False,
                    reason="assistant-confirm-failed",
                )

        delivery_state = outcome.delivery.value
        if delivery_state not in {"sent", "partial", "failed", "ambiguous"}:
            delivery_state = ""
        return _PreparedPendingTranscript(
            source_type=source_type,
            channel=channel,
            turn_id=turn_id,
            thread_key=thread_key,
            primary_source_event_id=source_ids[0],
            users=tuple(users),
            assistant_redaction=assistant_redaction,
            assistant_quarantine=assistant_quarantine,
            assistant_occurred_at=assistant_occurred_at,
            delivery_state=delivery_state,
            delivered_chunks=outcome.delivered_chunks,
            total_chunks=outcome.total_chunks,
            model_response_ref=outcome.model_response_ref,
        )
    except Exception:
        logger.exception(
            "drain_pending: transcript preparation failed tenant=%s channel=%s",
            str(tenant.id)[:8],
            batch[0].channel,
        )
        capture_error = RedactionOutcome(text="", confirmed=False, reason="capture-error")
        return _PreparedPendingTranscript(
            source_type=source_type,
            channel=channel,
            turn_id=turn_id,
            thread_key=thread_key,
            primary_source_event_id=source_ids[0],
            users=tuple(
                _PreparedPendingUserTranscript(
                    source_event_id=source_event_id,
                    occurred_at=row.created_at,
                    quarantine=capture_error,
                )
                for row, source_event_id in zip(ordered, source_ids, strict=True)
            ),
            assistant_quarantine=(capture_error if include_assistant and outcome.assistant_text else None),
            assistant_occurred_at=assistant_occurred_at,
            delivery_state=(
                outcome.delivery.value if outcome.delivery.value in {"sent", "partial", "failed", "ambiguous"} else ""
            ),
            delivered_chunks=outcome.delivered_chunks,
            total_chunks=outcome.total_chunks,
            model_response_ref=outcome.model_response_ref,
        )


def _persist_pending_transcript(tenant: Tenant, prepared: _PreparedPendingTranscript) -> None:
    """Write a prepared queue turn inside its caller's durable transaction."""
    from apps.transcripts.capture import capture_transcript_event, quarantine_transcript_event
    from apps.transcripts.models import TranscriptEvent

    for user in prepared.users:
        if user.redaction is not None:
            capture_transcript_event(
                tenant=tenant,
                source_type=prepared.source_type,
                source_event_id=user.source_event_id,
                role=TranscriptEvent.Role.USER,
                channel=prepared.channel,
                turn_id=prepared.turn_id,
                occurred_at=user.occurred_at,
                redaction=user.redaction,
                thread_key=prepared.thread_key,
            )
        else:
            quarantine_transcript_event(
                tenant=tenant,
                source_type=prepared.source_type,
                source_event_id=user.source_event_id,
                channel=prepared.channel,
                outcome=user.quarantine,
                turn_id=prepared.turn_id,
                occurred_at=user.occurred_at,
                thread_key=prepared.thread_key,
            )

    if prepared.assistant_redaction is not None:
        capture_transcript_event(
            tenant=tenant,
            source_type=TranscriptEvent.SourceType.ASSISTANT_REPLY,
            source_event_id=prepared.primary_source_event_id,
            role=TranscriptEvent.Role.ASSISTANT,
            channel=prepared.channel,
            turn_id=prepared.turn_id,
            occurred_at=prepared.assistant_occurred_at,
            redaction=prepared.assistant_redaction,
            thread_key=prepared.thread_key,
            delivery_state=prepared.delivery_state,
            delivered_chunks=prepared.delivered_chunks,
            total_chunks=prepared.total_chunks,
            model_response_ref=prepared.model_response_ref,
        )
    elif prepared.assistant_quarantine is not None:
        quarantine_transcript_event(
            tenant=tenant,
            source_type=TranscriptEvent.SourceType.ASSISTANT_REPLY,
            source_event_id=prepared.primary_source_event_id,
            channel=prepared.channel,
            outcome=prepared.assistant_quarantine,
            turn_id=prepared.turn_id,
            occurred_at=prepared.assistant_occurred_at,
            thread_key=prepared.thread_key,
        )


def _persist_pending_transcript_fail_open(
    tenant: Tenant,
    prepared: _PreparedPendingTranscript | None,
) -> None:
    if prepared is None:
        return
    try:
        # A savepoint keeps an unexpected ledger failure from poisoning the
        # surrounding delivery-state transaction.
        with transaction.atomic():
            _persist_pending_transcript(tenant, prepared)
    except Exception:
        try:
            from apps.pii.redactor import RedactionOutcome
            from apps.transcripts.capture import quarantine_transcript_event
            from apps.transcripts.models import TranscriptEvent

            capture_error = RedactionOutcome(text="", confirmed=False, reason="capture-error")
            with transaction.atomic():
                for user in prepared.users:
                    quarantine_transcript_event(
                        tenant=tenant,
                        source_type=prepared.source_type,
                        source_event_id=user.source_event_id,
                        channel=prepared.channel,
                        outcome=capture_error,
                        turn_id=prepared.turn_id,
                        occurred_at=user.occurred_at,
                        thread_key=prepared.thread_key,
                    )
                if prepared.assistant_redaction is not None or prepared.assistant_quarantine is not None:
                    quarantine_transcript_event(
                        tenant=tenant,
                        source_type=TranscriptEvent.SourceType.ASSISTANT_REPLY,
                        source_event_id=prepared.primary_source_event_id,
                        channel=prepared.channel,
                        outcome=capture_error,
                        turn_id=prepared.turn_id,
                        occurred_at=prepared.assistant_occurred_at,
                        thread_key=prepared.thread_key,
                    )
        except Exception:
            logger.exception(
                "drain_pending: transcript persistence failed tenant=%s channel=%s",
                str(tenant.id)[:8],
                prepared.channel,
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
) -> str:
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
            return "ceiling_raised"
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
                return "circuit_tripped"
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
    return "circuit_tripped"


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
    *,
    defer_publish_until_commit: bool = False,
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

    NOTE: callers must do channel-specific preprocessing (workspace
    routing, datetime context injection, inbound PII redaction of the
    user's text, etc.) BEFORE enqueuing — the redacted text is what gets
    stored on the row and forwarded. The Telegram poller does this in
    ``TelegramPoller._forward_to_container``. The queue itself is dumb on
    purpose — it just POSTs the prepared payload at the container and
    relays the reply, so it never re-runs redaction over the assembled
    body (which carries structural markers the NER detector would
    misfire on).

    ``defer_publish_until_commit`` is reserved for the dropped-turn replay:
    that path atomically returns the existing app row to PENDING and inserts
    this queue row, then publishes only after both writes commit. Ordinary
    inbound callers keep the historical immediate-publish behavior.
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

    def _publish_drain() -> None:
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

    if defer_publish_until_commit:
        transaction.on_commit(_publish_drain)
    else:
        _publish_drain()

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


def _row_is_image(msg: PendingMessage) -> bool:
    """A row is "image" if its payload carries ``is_image=True``.

    Image rows are forced singletons for a load-bearing reason: the
    ``[Photo attached: <path>]`` marker lives in ``payload.message_text``,
    but a coalesced batch (``len > 1``) rebuilds content from each row's
    ``user_text`` — which carries NO marker (see ``_build_batch_chat_content``).
    Folding an image row into a coalesced batch would therefore silently drop
    the photo. Keeping it singular keeps the marker on the wire.
    """
    payload = msg.payload or {}
    return bool(payload.get("is_image"))


def _row_is_document(msg: PendingMessage) -> bool:
    """A row is "document" if its payload carries ``is_document=True``.

    Same load-bearing reason as ``_row_is_image``: the ``[Document attached:
    <path>]`` marker lives only in ``payload.message_text``, and a coalesced
    rebuild from ``user_text`` would drop it — so a PDF row must stay a
    singleton or the agent never sees the document path.
    """
    payload = msg.payload or {}
    return bool(payload.get("is_document"))


def _row_is_singleton_media(msg: PendingMessage) -> bool:
    """Rows that must never fold into a coalesced text batch (voice/image/PDF)."""
    return _row_is_voice(msg) or _row_is_image(msg) or _row_is_document(msg)


def _claim_pending_batch_for_key(
    tenant: Tenant,
    channel: str,
    channel_user_id: str,
    timeout_seconds: float,
    *,
    break_live_lease_snapshot: list[tuple[Any, Any]] | None = None,
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
      - ``info["live_lease_snapshot"]``: exact row/expiry pairs that
        prevented a claim. The caller may probe liveness outside this
        transaction and pass the snapshot back for an atomic compare-and-set
        lease break followed by the normal claim.

    Batch composition rules (preserves the per-key single-turn invariant
    that prevents the OpenClaw claude-cli backend from rejecting
    overlapping turns):

      - Always starts with the oldest unleased PENDING row.
      - Subsequent contiguous rows are folded in as long as they are
        fresh (under stale threshold), under the attempts cap, and not
        singleton media (voice/image). The batch breaks at the first row
        that fails any of these — that row stays PENDING and gets handled
        on the next drain tick.
      - Voice/image rows are always singletons: if the head row is one,
        the batch is ``[that_row]``; otherwise such a row in the tail ends
        the batch (its marker-bearing message_text must not be coalesced
        away — see ``_row_is_image``).

    All claim + lease writes happen inside one
    ``SELECT ... FOR UPDATE SKIP LOCKED`` transaction so a concurrent
    drain task that observes a leased head row also sees the rest of
    the batch as leased — no two drain tasks ever build overlapping
    batches for the same key.
    """
    lease_seconds = timeout_seconds * _IN_FLIGHT_LEASE_FACTOR

    with transaction.atomic():
        claim_info: dict[str, Any] = {}
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
        live_lease_qs = PendingMessage.objects.filter(
            tenant=tenant,
            channel=channel,
            channel_user_id=channel_user_id or "",
            delivery_status=PendingMessage.Status.PENDING,
            delivery_in_flight_until__gt=now,
        )
        live_lease_snapshot = list(
            live_lease_qs.values_list(
                "id",
                "delivery_in_flight_until",
            )
        )
        if live_lease_snapshot:
            live_lease_expiry = max(expiry for _, expiry in live_lease_snapshot)
            if break_live_lease_snapshot is None:
                return (
                    [],
                    {
                        "live_lease_expiry": live_lease_expiry,
                        "live_lease_snapshot": live_lease_snapshot,
                    },
                )

            # The liveness probe happened before this transaction. Only break
            # the exact row+expiry pairs it observed: if another drain acquired
            # a replacement lease while the probe was in flight, it remains
            # protected and the re-check below defers behind it.
            break_filter = models.Q()
            for row_id, lease_expiry in break_live_lease_snapshot:
                break_filter |= models.Q(
                    id=row_id,
                    delivery_in_flight_until=lease_expiry,
                )
            broken_lease_count = live_lease_qs.filter(break_filter).update(
                delivery_in_flight_until=None,
            )
            if broken_lease_count:
                claim_info.update(
                    {
                        "broken_lease_count": broken_lease_count,
                        "broken_lease_expiry": max(expiry for _, expiry in break_live_lease_snapshot),
                    }
                )

            remaining_live_lease_snapshot = list(
                live_lease_qs.values_list(
                    "id",
                    "delivery_in_flight_until",
                )
            )
            if remaining_live_lease_snapshot:
                claim_info["live_lease_expiry"] = max(expiry for _, expiry in remaining_live_lease_snapshot)
                claim_info["live_lease_snapshot"] = remaining_live_lease_snapshot
                return ([], claim_info)

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
            return ([], claim_info)

        head = rows[0]

        # Past-cap head → caller drops + apologizes. No lease.
        if head.delivery_attempts >= _MAX_DELIVERY_ATTEMPTS:
            claim_info["past_cap_head"] = head
            return ([], claim_info)

        # Stale head → take lease so the FAILED-flip is uncontended.
        if head.created_at < stale_cutoff:
            head.delivery_in_flight_until = now + timedelta(seconds=lease_seconds)
            head.save(update_fields=["delivery_in_flight_until"])
            claim_info["stale_head"] = head
            return ([], claim_info)

        # Voice/image head → singleton batch (singleton media is never coalesced).
        if _row_is_singleton_media(head):
            head.delivery_in_flight_until = now + timedelta(seconds=lease_seconds)
            head.save(update_fields=["delivery_in_flight_until"])
            return ([head], claim_info)

        # Build a contiguous head batch of fresh, under-cap, non-singleton-media rows.
        batch: list[PendingMessage] = [head]
        for row in rows[1:]:
            if row.delivery_attempts >= _MAX_DELIVERY_ATTEMPTS:
                break
            if row.created_at < stale_cutoff:
                break
            if _row_is_singleton_media(row):
                break
            batch.append(row)

        for row in batch:
            row.delivery_in_flight_until = now + timedelta(seconds=lease_seconds)
            row.save(update_fields=["delivery_in_flight_until"])

        return (batch, claim_info)


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


def dropped_retry_dedup_id(turn_id) -> str:
    """Return the QStash-safe deduplication id for one app-turn replay."""
    compact_id = str(turn_id).replace("-", "")
    return f"retry-dropped-app-{compact_id}"


def dropped_retry_health_dedup_id(turn_id) -> str:
    """Return the distinct QStash dedup id for the one health deferral."""
    compact_id = str(turn_id).replace("-", "")
    return f"retry-dropped-health-{compact_id}"


def _app_turn_has_newer_message(turn: AppChatMessage) -> bool:
    """Whether the thread already contains a strictly newer user turn."""
    return (
        AppChatMessage.objects.filter(thread_id=turn.thread_id)
        .exclude(id=turn.id)
        .filter(models.Q(created_at__gt=turn.created_at) | models.Q(created_at=turn.created_at, id__gt=turn.id))
        .exists()
    )


def _runtime_write_in_retry_window(turn: AppChatMessage, dropped_stamped_at) -> bool:
    """Whether the control plane recorded a mutation during this turn."""
    return RuntimeWriteActivity.objects.filter(
        tenant_id=turn.tenant_id,
        last_runtime_write_at__gte=turn.created_at,
        last_runtime_write_at__lte=dropped_stamped_at,
    ).exists()


def _dropped_retry_enabled(tenant: Tenant) -> bool:
    """Whether the fail-closed dropped-turn retry canary includes ``tenant``."""
    raw = str(getattr(settings, "RETRY_DROPPED_TENANT_IDS", "") or "")
    allowed = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not allowed:
        return False
    if str(tenant.id).lower() in allowed:
        return True
    logger.info(
        "retry_dropped: tenant %s is not in RETRY_DROPPED_TENANT_IDS (%d id(s) configured) — no retry",
        str(tenant.id)[:8],
        len(allowed),
    )
    return False


def _is_silent_dropped_retry_candidate(turn: AppChatMessage, error: str, *, dropped_stamped_at) -> bool:
    """Conservative enqueue predicate, evaluated before terminalizing."""
    # Four safety legs bound the accepted raw-cron residual: canary gate,
    # exact thinking phase, no authoritative runtime write, and drop age at
    # most 30 seconds. Follow-up guard lane: apps/cron/gateway_client.py's
    # deferred wrapping-plugin guard for OC-native raw cron.add.
    return bool(
        error == "dropped"
        and _dropped_retry_enabled(turn.tenant)
        and turn.source == AppChatMessage.Source.TENANT
        and turn.retried_at is None
        and turn.reply_text == ""
        and turn.partial_text == ""
        and turn.phase == _DROPPED_RETRY_SAFE_PHASE
        and not _runtime_write_in_retry_window(turn, dropped_stamped_at)
        and (dropped_stamped_at - turn.created_at).total_seconds() <= _DROPPED_RETRY_MAX_DROP_AGE_SECONDS
        and not _app_turn_has_newer_message(turn)
    )


def _notify_retry_exhausted(tenant: Tenant, turn_id, client_msg_id: str, reason: str) -> None:
    """Emit the terminal counter and existing generic app error notification."""
    logger.warning(
        "retry_exhausted count=1 tenant=%s turn=%s reason=%s",
        str(tenant.id)[:8],
        str(turn_id)[:16],
        reason,
    )
    from apps.router.push_views import notify_app_reply_error

    _dispatch_push(notify_app_reply_error, tenant, [client_msg_id])


def _enqueue_dropped_app_turn_retry(tenant: Tenant, turn_id, client_msg_id: str) -> None:
    """Publish the one delayed replay after the dropped transition commits."""
    try:
        from apps.cron.publish import publish_task

        publish_task(
            "retry_dropped_app_turn",
            str(turn_id),
            idempotency_key=dropped_retry_dedup_id(turn_id),
            delay_seconds=_DROPPED_RETRY_DELAY_SECONDS,
            retries=0,
        )
        logger.info(
            "retry_enqueued count=1 tenant=%s turn=%s delay_seconds=%d",
            str(tenant.id)[:8],
            str(turn_id)[:16],
            _DROPPED_RETRY_DELAY_SECONDS,
        )
    except Exception:
        logger.exception(
            "retry_exhausted count=1 tenant=%s turn=%s reason=publish_failed",
            str(tenant.id)[:8],
            str(turn_id)[:16],
        )
        from apps.router.push_views import notify_app_reply_error

        _dispatch_push(notify_app_reply_error, tenant, [client_msg_id])


def _terminalize_pending_app_turns(
    tenant: Tenant,
    client_msg_ids: list[str],
    error: str,
    *,
    now,
) -> list[str]:
    """Compare-and-set correlated app turns from PENDING to ERROR.

    Each id is updated separately so the on-commit notification contains only
    turns this call actually transitioned. That keeps repeat terminalization
    idempotent without locking app rows or overwriting a concurrently completed
    READY turn.
    """
    from apps.router.push_views import notify_app_reply_error

    terminalized_ids: list[str] = []
    notify_error_ids: list[str] = []
    retries_to_enqueue: list[tuple[object, str]] = []
    exhausted_retries: list[tuple[object, str, str]] = []
    for client_msg_id in dict.fromkeys(client_msg_ids):
        turn = (
            AppChatMessage.objects.select_for_update()
            .filter(
                tenant=tenant,
                client_msg_id=client_msg_id,
                status=AppChatMessage.Status.PENDING,
            )
            .first()
        )
        if turn is None:
            continue
        should_retry = _is_silent_dropped_retry_candidate(turn, error, dropped_stamped_at=now)
        update_values = {
            "status": AppChatMessage.Status.ERROR,
            "error": error,
            "replied_at": now,
            "partial_text": "",
        }
        if should_retry:
            # Spending the replay is atomic with the dropped transition. A
            # duplicate terminalizer can neither enqueue nor spend it twice.
            update_values["retried_at"] = now
        changed = AppChatMessage.objects.filter(
            id=turn.id,
            status=AppChatMessage.Status.PENDING,
        ).update(**update_values)
        if changed:
            terminalized_ids.append(client_msg_id)
            if should_retry:
                retries_to_enqueue.append((turn.id, client_msg_id))
            elif turn.retried_at is not None:
                exhausted_retries.append((turn.id, client_msg_id, error))
            else:
                notify_error_ids.append(client_msg_id)

    if notify_error_ids:
        transaction.on_commit(
            lambda tenant=tenant, ids=list(notify_error_ids): _dispatch_push(
                notify_app_reply_error,
                tenant,
                ids,
            )
        )
    for turn_id, client_msg_id in retries_to_enqueue:
        transaction.on_commit(
            lambda tenant=tenant, turn_id=turn_id, client_msg_id=client_msg_id: _enqueue_dropped_app_turn_retry(
                tenant,
                turn_id,
                client_msg_id,
            )
        )
    for turn_id, client_msg_id, terminal_reason in exhausted_retries:
        transaction.on_commit(
            lambda tenant=tenant, turn_id=turn_id, client_msg_id=client_msg_id, reason=terminal_reason: (
                _notify_retry_exhausted(
                    tenant,
                    turn_id,
                    client_msg_id,
                    reason,
                )
            )
        )
    return terminalized_ids


def _terminalize_failed_queue_rows(
    *,
    tenant: Tenant | None,
    tenant_id: str,
    channel: str,
    channel_user_id: str,
    error: str,
    row_ids: list | None = None,
    require_missing_fqdn: bool = False,
    send_apology: bool = True,
    # Intentionally None for stale-head, past-cap, and provisioning-timeout:
    # no completed turn exists, so P1 excludes those FAILED, repairable rows.
    transcript_capture: _PreparedPendingTranscript | None = None,
) -> tuple[Tenant | None, list[PendingMessage], bool]:
    """Atomically fail pending queue rows and correlated pending app turns.

    Returns ``(tenant, rows, fqdn_appeared)``. The final flag is used by the
    no-FQDN guard: provisioning may have completed after its initial read, in
    which case nothing is failed and the caller continues through normal drain.
    Channel-native apologies run only after the transaction commits; APNs is
    registered through ``transaction.on_commit`` by the app-turn CAS above.
    """
    locked_tenant = tenant
    terminalized_rows: list[PendingMessage] = []
    fqdn_appeared = False
    committed_at = None

    with transaction.atomic():
        if require_missing_fqdn:
            locked_tenant = (
                Tenant.objects.select_for_update(of=("self",)).select_related("user").filter(id=tenant_id).first()
            )
            if locked_tenant is None:
                return None, [], False
            if locked_tenant.container_fqdn:
                return locked_tenant, [], True

        if locked_tenant is None:
            return None, [], False

        queue_rows = PendingMessage.objects.select_for_update().filter(
            tenant_id=tenant_id,
            channel=channel,
            channel_user_id=channel_user_id or "",
            delivery_status=PendingMessage.Status.PENDING,
        )
        if row_ids is not None:
            queue_rows = queue_rows.filter(id__in=row_ids)
        terminalized_rows = list(queue_rows.order_by("created_at"))
        if not terminalized_rows:
            return locked_tenant, [], False

        committed_at = timezone.now()
        client_msg_ids = _ios_client_msg_ids(terminalized_rows)
        if client_msg_ids:
            _terminalize_pending_app_turns(
                locked_tenant,
                client_msg_ids,
                error,
                now=committed_at,
            )

        locked_ids = [row.id for row in terminalized_rows]
        PendingMessage.objects.filter(
            id__in=locked_ids,
            delivery_status=PendingMessage.Status.PENDING,
        ).update(
            delivery_status=PendingMessage.Status.FAILED,
            delivered_at=committed_at,
            delivery_in_flight_until=None,
        )
        for row in terminalized_rows:
            row.delivery_status = PendingMessage.Status.FAILED
            row.delivered_at = committed_at
            row.delivery_in_flight_until = None

        _persist_pending_transcript_fail_open(locked_tenant, transcript_capture)

    # LINE/Telegram sends are external side effects, so they must not happen
    # until both model transitions have committed successfully.
    if send_apology:
        for row in terminalized_rows:
            if row.channel == PendingMessage.Channel.IOS:
                continue
            if error == "stale":
                age_seconds = max(0.0, (committed_at - row.created_at).total_seconds())
                _send_apology_for_stale_pending_message(locked_tenant, row, age_seconds)
            else:
                _send_apology_for_dropped_pending_message(locked_tenant, row)

    return locked_tenant, terminalized_rows, fqdn_appeared


def retry_dropped_app_turn_task(turn_id: str) -> dict:
    """Spend one delayed replay by re-entering the normal iOS queue path.

    The row was stamped ``retried_at`` when its original PendingMessage was
    finalized dropped. This task claims its own inbound event, re-checks every
    conservative predicate after the 60-second delay, verifies the container is
    serving, then atomically returns the same AppChatMessage to PENDING and
    copies the original failed queue payload into a fresh PendingMessage.
    """
    turn = AppChatMessage.objects.select_related("tenant", "user", "thread").filter(id=turn_id).first()
    if turn is None:
        logger.warning(
            "retry_exhausted count=1 tenant=unknown turn=%s reason=missing_turn",
            str(turn_id)[:16],
        )
        return {"retried": 0, "exhausted": "missing_turn"}

    tenant = turn.tenant

    def _exhaust(reason: str) -> dict:
        _notify_retry_exhausted(tenant, turn.id, turn.client_msg_id, reason)
        return {"retried": 0, "exhausted": reason}

    try:
        if turn.status == AppChatMessage.Status.PENDING and turn.retried_at is not None:
            return {"retried": 0, "duplicate": True}
        # The canary gate is mutable and must still authorize the replay when
        # this delayed task fires. Drop age is immutable once ``retried_at``
        # stamps the original drop, so it does not need a fire-time re-check.
        if not _dropped_retry_enabled(tenant):
            return _exhaust("gate_revoked")
        if (
            turn.status != AppChatMessage.Status.ERROR
            or turn.error != "dropped"
            or turn.retried_at is None
            or turn.source != AppChatMessage.Source.TENANT
            or turn.reply_text != ""
            or turn.phase != _DROPPED_RETRY_SAFE_PHASE
            or _runtime_write_in_retry_window(turn, turn.retried_at)
        ):
            # A late original reply may have won the race and moved the row READY;
            # that is already user-visible success, not exhaustion.
            if turn.status == AppChatMessage.Status.READY:
                return {"retried": 0, "already_ready": True}
            return _exhaust("ineligible")
        if _app_turn_has_newer_message(turn):
            return _exhaust("newer_turn")

        if not tenant.container_fqdn or not _is_tenant_container_live(tenant):
            deferred_at = timezone.now()
            deferred = AppChatMessage.objects.filter(
                id=turn.id,
                status=AppChatMessage.Status.ERROR,
                retry_health_deferred_at__isnull=True,
            ).update(retry_health_deferred_at=deferred_at)
            if deferred:
                from apps.cron.publish import publish_task

                publish_task(
                    "retry_dropped_app_turn",
                    str(turn.id),
                    idempotency_key=dropped_retry_health_dedup_id(turn.id),
                    delay_seconds=_DROPPED_RETRY_HEALTH_DELAY_SECONDS,
                    retries=0,
                )
                logger.info(
                    "retry_redeferred count=1 tenant=%s turn=%s delay_seconds=%d",
                    str(tenant.id)[:8],
                    str(turn.id)[:16],
                    _DROPPED_RETRY_HEALTH_DELAY_SECONDS,
                )
                return {"retried": 0, "deferred": "container_unhealthy"}
            return _exhaust("container_unhealthy")

        # Claim only after eligibility and health checks. A worker crash before
        # this point must not burn the durable delivery claim.
        from apps.router.inbound_dedup import claim_inbound_event

        if not claim_inbound_event(f"app-retry:{turn.id}"):
            return {"retried": 0, "duplicate": True}

        failure_reason = ""
        duplicate_after_lock = False
        with transaction.atomic():
            # App ingress locks this row before creating every user turn. Holding
            # it through the final recency check and requeue makes "newest" an
            # invariant rather than a check-then-insert race.
            ChatThread.objects.select_for_update().only("id").get(id=turn.thread_id)
            locked_turn = AppChatMessage.objects.select_for_update().select_related("tenant").filter(id=turn.id).first()
            if locked_turn is None:
                failure_reason = "missing_turn"
            elif locked_turn.status != AppChatMessage.Status.ERROR:
                # This status transition is the real fail-open/double-delivery
                # guard. ``retried_at`` is already set before either delivery.
                duplicate_after_lock = True
            elif (
                locked_turn.error != "dropped"
                or locked_turn.retried_at is None
                or locked_turn.reply_text != ""
                or locked_turn.phase != _DROPPED_RETRY_SAFE_PHASE
                or _runtime_write_in_retry_window(locked_turn, locked_turn.retried_at)
                or _app_turn_has_newer_message(locked_turn)
            ):
                failure_reason = "ineligible_after_health"
            else:
                source_row = (
                    PendingMessage.objects.select_for_update()
                    .annotate(payload_client_msg_id=KeyTextTransform("client_msg_id", "payload"))
                    .filter(
                        tenant=tenant,
                        channel=PendingMessage.Channel.IOS,
                        delivery_status=PendingMessage.Status.FAILED,
                        payload_client_msg_id=locked_turn.client_msg_id,
                    )
                    .order_by("-delivered_at", "-created_at")
                    .first()
                )
                if source_row is None:
                    failure_reason = "missing_original_inbound"
                else:
                    locked_turn.status = AppChatMessage.Status.PENDING
                    locked_turn.error = ""
                    locked_turn.replied_at = None
                    locked_turn.waking_at = None
                    locked_turn.phase = ""
                    locked_turn.phase_detail = ""
                    locked_turn.partial_text = ""
                    locked_turn.notified_at = None
                    locked_turn.save(
                        update_fields=[
                            "status",
                            "error",
                            "replied_at",
                            "waking_at",
                            "phase",
                            "phase_detail",
                            "partial_text",
                            "notified_at",
                        ]
                    )
                    enqueue_message_for_tenant(
                        tenant=tenant,
                        channel=PendingMessage.Channel.IOS,
                        channel_user_id=str(locked_turn.thread_id),
                        payload=dict(source_row.payload or {}),
                        user_text_excerpt=source_row.user_text,
                        defer_publish_until_commit=True,
                    )

        if duplicate_after_lock:
            return {"retried": 0, "duplicate": True}
        if failure_reason:
            return _exhaust(failure_reason)
        return {"retried": 1}
    except Exception:
        logger.exception(
            "retry_task_error tenant=%s turn=%s",
            str(tenant.id)[:8],
            str(turn.id)[:16],
        )
        return _exhaust("task_error")


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
        # A missing FQDN means one of two very different things, and they
        # must NOT be treated the same:
        #   (1) container still being BUILT — a brand-new signup whose
        #       per-tenant container is mid-provision (status provisioning/
        #       pending). The FQDN is coming online shortly. Failing the
        #       message here silently strands a new user's very first
        #       "Hello": it goes FAILED, the reaper only re-drives PENDING
        #       rows, and nothing re-delivers it when the container lands.
        #       Instead buffer it the same way the hibernation-wake path
        #       does — keep the rows PENDING, surface "waking" to rich
        #       clients, and re-drive after a short delay so it delivers the
        #       moment the container is up.
        #   (2) container GONE — no tenant row, or deprovisioned/suspended/
        #       deleted. The original intent of this guard: nothing to wait
        #       for, so fail orphaned rows and stop re-scheduling.
        provisioning = tenant is not None and tenant.status in (
            Tenant.Status.PROVISIONING,
            Tenant.Status.PENDING,
        )
        if provisioning:
            pending_rows = list(
                PendingMessage.objects.filter(
                    tenant_id=tenant_id,
                    channel=channel,
                    channel_user_id=channel_user_id or "",
                    delivery_status=PendingMessage.Status.PENDING,
                ).order_by("created_at")
            )
            oldest = pending_rows[0].created_at if pending_rows else None
            within_wait = oldest is not None and (timezone.now() - oldest).total_seconds() < _PROVISION_MAX_WAIT_SECONDS
            if within_wait:
                logger.info(
                    "drain_pending: tenant %s still provisioning (no FQDN) — buffering %d msg(s), deferring drain %ds",
                    tenant_id[:8],
                    len(pending_rows),
                    _WAKE_DEFER_SECONDS,
                )
                # Rich clients (iOS) render "your assistant is waking up"
                # off waking_at instead of an indefinite typing spinner.
                _mark_ios_waking(channel, pending_rows)
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
                    "provisioning": True,
                }
            # Provisioning has run well past the expected window (stuck or
            # failed provision). Fall through and fail so we don't defer
            # forever — the repair-stale-provisioning cron owns recovery.
            logger.warning(
                "drain_pending: tenant %s provisioning exceeded %ds — failing queued msg(s)",
                tenant_id[:8],
                _PROVISION_MAX_WAIT_SECONDS,
            )

        # Re-lock/re-read the tenant with the queue rows. Provisioning may have
        # completed after the initial read; if an FQDN appeared, continue into
        # normal drain instead of terminalizing a now-deliverable turn.
        locked_tenant, terminalized_rows, fqdn_appeared = _terminalize_failed_queue_rows(
            tenant=tenant,
            tenant_id=tenant_id,
            channel=channel,
            channel_user_id=channel_user_id or "",
            error="dropped",
            require_missing_fqdn=True,
        )
        if not fqdn_appeared:
            logger.warning(
                "drain_pending: tenant %s not found or no FQDN, dropping queue",
                tenant_id[:8],
            )
            return {
                "delivered": 0,
                "failed": 0,
                "dropped": len(terminalized_rows),
                "skipped_in_flight": 0,
            }
        tenant = locked_tenant

    chat_timeout = _resolve_chat_timeout(tenant)
    batch, info = _claim_pending_batch_for_key(tenant, channel, channel_user_id or "", chat_timeout)

    # A live per-key lease normally means another drain is mid-turn. Before
    # trusting it, probe the container OUTSIDE the claim transaction. A lease
    # against a down container cannot be making progress; clear only the
    # leases observed by the first claim attempt, then clear + claim atomically
    # in a fresh transaction. A newer concurrent lease remains protected.
    live_lease_expiry = info.get("live_lease_expiry")
    live_lease_snapshot = info.get("live_lease_snapshot")
    if live_lease_expiry is not None and not _is_tenant_container_live(tenant):
        batch, info = _claim_pending_batch_for_key(
            tenant,
            channel,
            channel_user_id or "",
            chat_timeout,
            break_live_lease_snapshot=live_lease_snapshot,
        )
        broken_lease_count = info.get("broken_lease_count", 0)
        if broken_lease_count:
            logger.error(
                "drain_pending: BROKE %d live in-flight lease(s) against DOWN container "
                "for tenant %s key=%s/%s (overrode expiry %s); claiming in same tick",
                broken_lease_count,
                tenant_id[:8],
                channel,
                (channel_user_id or "")[:24],
                info["broken_lease_expiry"].isoformat(),
            )

    # Past-cap head — no lease taken; drop + apologize + reschedule if more.
    past_cap_head = info.get("past_cap_head")
    if past_cap_head is not None:
        logger.warning(
            "drain_pending: dropping msg %s for tenant %s after %d attempts",
            past_cap_head.id,
            tenant_id[:8],
            past_cap_head.delivery_attempts,
        )
        _, terminalized_rows, _ = _terminalize_failed_queue_rows(
            tenant=tenant,
            tenant_id=tenant_id,
            channel=channel,
            channel_user_id=channel_user_id or "",
            error="dropped",
            row_ids=[past_cap_head.id],
        )
        if _has_more_pending(tenant, channel, channel_user_id or ""):
            _reschedule_drain(tenant, channel, channel_user_id or "")
        return {
            "delivered": 0,
            "failed": 0,
            "dropped": len(terminalized_rows),
            "skipped_in_flight": 0,
        }

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
        _, terminalized_rows, _ = _terminalize_failed_queue_rows(
            tenant=tenant,
            tenant_id=tenant_id,
            channel=channel,
            channel_user_id=channel_user_id or "",
            error="stale",
            row_ids=[stale_head.id],
        )
        if _has_more_pending(tenant, channel, channel_user_id or ""):
            _reschedule_drain(tenant, channel, channel_user_id or "")
        return {
            "delivered": 0,
            "failed": 0,
            "dropped": len(terminalized_rows),
            "skipped_in_flight": 0,
            "stale": 1,
        }

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
            outcome = _drain_line_batch(tenant, batch, chat_timeout)
        elif channel == PendingMessage.Channel.TELEGRAM:
            outcome = _drain_telegram_batch(tenant, batch, chat_timeout)
        elif channel == PendingMessage.Channel.IOS:
            outcome = _drain_ios_batch(tenant, batch, chat_timeout)
        else:
            raise ValueError(f"Unknown channel: {channel!r}")
        gateway_responded = outcome.gateway_responded

        if outcome.disposition is Disposition.DELIVER:
            if outcome.delivery is DeliveryState.PARTIAL:
                logger.warning(
                    "drain_pending: partial delivery for tenant %s channel=%s (%d/%d chunks); "
                    "marking delivered to avoid duplicating chunks already seen",
                    tenant_id[:8],
                    channel,
                    outcome.delivered_chunks,
                    outcome.total_chunks,
                )
            elif outcome.delivery is DeliveryState.AMBIGUOUS:
                logger.warning(
                    "drain_pending: ambiguous delivery for tenant %s channel=%s; marking delivered because "
                    "retrying risks a duplicate reply",
                    tenant_id[:8],
                    channel,
                )

            transcript_capture = _prepare_pending_transcript(
                tenant,
                batch,
                outcome,
                include_assistant=True,
            )
            now = timezone.now()
            with transaction.atomic():
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
                _persist_pending_transcript_fail_open(tenant, transcript_capture)
            delivered = batch_size

            # Privacy hard-delete (docs/encryption-at-rest-directive.md §7, Phase 0
            # PR-3): PendingMessage is a transient forwarding queue, but delivered
            # rows were never removed — leaving payload + user_text as a permanent
            # store of (redacted) user text. Every downstream use of these rows is
            # complete by here: the _drain_*_batch helper above ran the OC POST,
            # reply relay, conversation capture, iOS reply persistence (to
            # AppChatMessage), and usage recording in the same call. Nothing past
            # this point re-reads the rows — the stale-hibernation reconcile touches
            # the Tenant; _has_more_pending queries PENDING rows only; the iOS
            # client polls AppChatMessage, not this queue. We mark DELIVERED *first*
            # (committed above) so a worker crash before the delete leaves a
            # sweepable DELIVERED row rather than a PENDING one the reaper would
            # re-POST (duplicate reply); the 14-day cleanup_stale_pending_messages
            # cron sweeps any such residue.
            try:
                PendingMessage.objects.filter(id__in=[row.id for row in batch]).delete()
            except Exception:
                logger.exception(
                    "drain_pending: failed to hard-delete delivered batch for tenant %s "
                    "(rows remain DELIVERED; 14-day cleanup cron will sweep)",
                    tenant_id[:8],
                )
        elif outcome.disposition is Disposition.RETRY:
            logger.warning(
                "drain_pending: retrying batch of %d for tenant %s channel=%s reason=%s",
                batch_size,
                tenant_id[:8],
                channel,
                outcome.reason,
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
        else:
            transcript_capture = _prepare_pending_transcript(
                tenant,
                batch,
                outcome,
                include_assistant=False,
            )
            _, terminalized_rows, _ = _terminalize_failed_queue_rows(
                tenant=tenant,
                tenant_id=tenant_id,
                channel=channel,
                channel_user_id=channel_user_id or "",
                error=outcome.reason,
                send_apology=False,
                transcript_capture=transcript_capture,
            )
            return {
                "delivered": 0,
                "failed": len(terminalized_rows),
                "dropped": 0,
                "skipped_in_flight": 0,
                "terminal": outcome.reason,
                "batch_size": batch_size,
            }

    except Exception as exc:
        container_down = _delivery_failure_has_down_container(tenant, exc)
        proxy_gateway_down = _delivery_failure_is_proxy_gateway_down(exc)
        recoverable_boot_failure = container_down or proxy_gateway_down
        if recoverable_boot_failure:
            # Idle hibernation can race a drain that loaded the tenant just
            # before hibernated_at was stamped. Refresh the two recovery fields
            # after the failed POST so container-down and proxy-gateway failures
            # see the current wake/hibernate state.
            try:
                tenant.refresh_from_db(fields=["hibernated_at", "last_wake_at"])
            except Exception:
                logger.exception(
                    "drain_pending: failed to refresh container recovery state for tenant %s",
                    tenant_id[:8],
                )

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
        # re-drain. Gated on a direct 404 or a timeout/network/protocol error
        # whose follow-up health probe also says down, so a genuine turn
        # timeout against a live container still flows through the normal
        # bounded retry path.
        if tenant.hibernated_at is not None and container_down:
            from apps.billing.services import check_budget

            # Mirror the webhook's "budget check before wake": never re-wake
            # a tenant hibernated for being over budget — that would un-gate
            # spend. Over-budget messages fall through to the normal
            # attempt-cap path (drop + apologize).
            if not check_budget(tenant):
                from apps.orchestrator.hibernation import wake_hibernated_tenant

                # Only defer-without-burning-an-attempt when the wake actually
                # succeeded. If wake_hibernated_tenant returns False (e.g. a
                # transient Azure error, or a non-already-active revision
                # conflict that propagated past the idempotent-activate guard),
                # deferring for free would re-arm the exact 404→wake→fail loop
                # forever — the message would never deliver and never cap out.
                # On a failed wake we fall through to the bounded failure path
                # below, which advances the attempt counter and eventually
                # drops + apologizes. (canary 148ccf1c, 2026-06-25)
                woke = wake_hibernated_tenant(tenant)
                if woke:
                    for row in batch:
                        row.delivery_in_flight_until = None
                        row.save(update_fields=["delivery_in_flight_until"])
                    logger.info(
                        "drain_pending: tenant %s hibernated (container down) — woke and deferring drain %ds",
                        tenant_id[:8],
                        _WAKE_DEFER_SECONDS,
                    )
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
                logger.warning(
                    "drain_pending: tenant %s wake attempt failed — falling through to bounded retry",
                    tenant_id[:8],
                )

        # Boot grace: the container was woken moments ago (this drain or the
        # webhook path) and either its replica or the gateway behind its proxy
        # isn't serving yet. Not the message's fault — release the lease, keep
        # the attempt counters, retry soon.
        # Without this, the shorter _WAKE_DEFER_SECONDS would burn all
        # _MAX_DELIVERY_ATTEMPTS during a slow cold boot.
        if (
            recoverable_boot_failure
            and tenant.last_wake_at is not None
            and (timezone.now() - tenant.last_wake_at).total_seconds() < _WAKE_BOOT_GRACE_SECONDS
        ):
            for row in batch:
                row.delivery_in_flight_until = None
                row.save(update_fields=["delivery_in_flight_until"])
            boot_state = "gateway still booting behind proxy" if proxy_gateway_down else "still booting after wake"
            logger.info(
                "drain_pending: tenant %s %s — deferring drain %ds (no attempt burned)",
                tenant_id[:8],
                boot_state,
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
        "delivery": outcome.delivery.value,
        "delivered_chunks": outcome.delivered_chunks,
        "total_chunks": outcome.total_chunks,
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


def _is_tenant_container_live(tenant: Tenant) -> bool:
    """Cheaply probe whether the tenant container is serving.

    A deactivated Container Apps revision returns 404 at ingress. Transport
    timeouts, connection failures, connection resets, and a remote protocol
    close likewise mean the probe could not reach a serving replica. Any other
    HTTP response proves that a container answered, and an unexpected local
    probe error fails safe as "live" so we never break a potentially-progressing
    per-key turn on ambiguous evidence.

    This helper performs network I/O and must only be called outside
    ``transaction.atomic()``.
    """
    url = f"https://{tenant.container_fqdn}/health"
    try:
        response = httpx.get(url, timeout=_CONTAINER_HEALTH_TIMEOUT_SECONDS)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        logger.warning(
            "drain_pending: container health probe says DOWN for tenant %s (%s)",
            str(tenant.id)[:8],
            type(exc).__name__,
        )
        return False
    except Exception:
        logger.exception(
            "drain_pending: ambiguous container health probe failure for tenant %s; preserving live lease",
            str(tenant.id)[:8],
        )
        return True

    if response.status_code == 404:
        logger.warning(
            "drain_pending: container health probe returned 404 for tenant %s",
            str(tenant.id)[:8],
        )
        return False
    return True


def _delivery_failure_has_down_container(tenant: Tenant, exc: Exception) -> bool:
    """Resolve whether a failed delivery POST should enter wake/boot grace.

    A 404 is the direct deactivated-revision signal. Timeout-family failures,
    network errors (including connection resets), and remote protocol closes
    are ambiguous on their own: probe ``/health`` and only treat them as down
    when that independent signal also says no replica is serving.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 404
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return not _is_tenant_container_live(tenant)
    return False


def _delivery_failure_is_proxy_gateway_down(exc: Exception) -> bool:
    """Detect the tenant proxy's structured gateway-unreachable response."""
    try:
        if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 502:
            return False
        body = exc.response.json()
        return isinstance(body, dict) and body.get("error") == "bad_gateway"
    except Exception:
        # Defensive — malformed/unreadable responses stay generic failures.
        return False


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
    if suppresses_real_transport(tenant):
        logger.error(
            "eval-sink transport block: tenant=%s transport=telegram",
            tenant.id,
        )
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
    # iOS has no channel-native apology push: the client poll/since feed reads
    # AppChatMessage status, so flip the turn to ERROR and fire the generic
    # 'couldn't finish' APNs push (idempotent) — otherwise it spins forever.
    if msg.channel == PendingMessage.Channel.IOS:
        _store_ios_turn_error(tenant, [msg], "stale")
        return

    if suppresses_real_transport(tenant):
        logger.error(
            "eval-sink transport block: tenant=%s transport=%s",
            tenant.id,
            msg.channel,
        )
        return

    from apps.pii.redactor import rehydrate_for_tenant
    from apps.router.error_messages import error_msg

    excerpt = rehydrate_for_tenant(tenant, (msg.user_text or "")).strip().replace("\n", " ")
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
    # iOS has no channel-native apology push: the client poll/since feed reads
    # AppChatMessage status, so flip the turn to ERROR and fire the generic
    # 'couldn't finish' APNs push (idempotent) — otherwise it spins forever.
    if msg.channel == PendingMessage.Channel.IOS:
        _store_ios_turn_error(tenant, [msg], "dropped")
        return

    if suppresses_real_transport(tenant):
        logger.error(
            "eval-sink transport block: tenant=%s transport=%s",
            tenant.id,
            msg.channel,
        )
        return

    from apps.pii.redactor import rehydrate_for_tenant
    from apps.router.error_messages import error_msg, strip_internal_framing

    # Defense in depth: even though every call site is supposed to pass
    # ``raw_user_text`` so PendingMessage.user_text is clean, peel any
    # ``[System: \u2026]`` / ``[Now: \u2026]`` / ``[chat: \u2026]`` / ``[User tapped button: \u2026]``
    # framing off the head before quoting. The user shouldn't see this.
    # Rehydrate first so PII placeholders (e.g. [PERSON_1]) are replaced
    # with the user's own words before the excerpt is echoed back to them.
    excerpt = strip_internal_framing(rehydrate_for_tenant(tenant, msg.user_text or "")).strip().replace("\n", " ")
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


def _build_batch_chat_content(
    batch: list[PendingMessage], fallback_user_id: str, channel: str | None = None
) -> tuple[str, str, str]:
    """Build the ``content`` string + routing context for a deliverable batch.

    Returns ``(content, user_param, user_timezone)``.

    Singleton batches (``len(batch) == 1``) preserve the existing per-row
    on-the-wire shape except that a recognized ``[Now: ...]`` marker is
    rebuilt at drain time. This keeps load-bearing proactive, reply, voice,
    photo, and document framing from ``payload.message_text`` intact while
    preventing a container wake from making the clock stale.

    Coalesced batches (``len(batch) > 1``) build a fresh prompt at drain
    time using ``format_coalesced_user_content``: the datetime + coalesced
    chat marker are emitted ONCE (from the latest row's routing context),
    then each row's raw ``user_text`` is appended with an index +
    timestamp. The intent is the agent reads N delineated follow-ups
    instead of N separate per-turn replies. ``channel`` (the drain's own
    channel — ``"telegram"`` / ``"line"`` / ``"ios"``) is stamped into that
    rebuilt marker so the coalesced path keeps the same per-turn channel
    signal the singleton (producer-baked) path carries.
    """
    from apps.router.services import build_datetime_context, format_coalesced_user_content

    if len(batch) == 1:
        msg = batch[0]
        payload = msg.payload or {}
        content = payload.get("message_text") or ""
        user_param = payload.get("user_param") or msg.channel_user_id or fallback_user_id
        user_tz = payload.get("user_timezone") or "UTC"
        if payload.get("user_timezone") and isinstance(content, str):
            content = re.sub(
                r"(?m)^\[Now: \d{4}-\d{2}-\d{2} \d{2}:\d{2} [^\r\n]+ \([A-Za-z]+\)\]\r?\n",
                build_datetime_context(user_tz),
                content,
                count=1,
            )
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
        channel=channel,
    )
    return content, user_param, user_tz


def _drain_line_batch(tenant: Tenant, batch: list[PendingMessage], timeout: float) -> DrainOutcome:
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
        return DrainOutcome(
            Disposition.DELIVER,
            DeliveryState.FAILED,
            gateway_responded=False,
            reason="empty_batch",
        )

    from apps.router.line_webhook import relay_ai_response_to_line

    line_user_id = batch[0].channel_user_id
    content, user_param, user_tz = _build_batch_chat_content(batch, line_user_id, channel="line")
    from apps.pii.redactor import annotate_model_context

    content = annotate_model_context(content, getattr(tenant, "pii_entity_map", None))
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
        "X-OpenClaw-Message-Channel": "line",
    }

    resp = httpx.post(url, json=chat_payload, headers=headers, timeout=timeout)
    if _looks_like_openrouter_credit_limit(resp):
        credit_result = _handle_openrouter_credit_limit(tenant, channel="line", channel_user_id=line_user_id)
        return DrainOutcome(
            Disposition.RETRY if credit_result == "ceiling_raised" else Disposition.TERMINAL,
            DeliveryState.FAILED,
            gateway_responded=False,
            reason="ceiling_raised" if credit_result == "ceiling_raised" else "budget_exhausted",
        )
    resp.raise_for_status()
    result = resp.json()

    ai_text = _extract_ai_response(result)
    send_result = SendResult(DeliveryState.SENT, detail="empty_ai_text")
    if suppresses_real_transport(tenant):
        send_result = SendResult(DeliveryState.SUPPRESSED, detail="eval_sink")
    elif ai_text and line_user_id:
        try:
            sent = relay_ai_response_to_line(tenant, line_user_id, ai_text)
            send_result = SendResult(
                DeliveryState.SENT if sent else DeliveryState.FAILED,
                delivered_chunks=1 if sent else 0,
                total_chunks=1,
                detail="" if sent else "relay_failed",
            )
        except Exception:
            logger.exception(
                "drain_pending: failed to relay LINE response for tenant %s",
                str(tenant.id)[:8],
            )
            send_result = SendResult(
                DeliveryState.AMBIGUOUS,
                total_chunks=1,
                detail="relay_exception",
            )

    _capture_conversation_turn(tenant, "line", line_user_id, batch, ai_text)
    _record_usage_safe(tenant, result, message_count=len(batch))
    return _drain_outcome_from_send_result(
        send_result,
        assistant_text=ai_text or "",
        model_response_ref=_model_response_ref(result),
    )


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


def _drain_telegram_batch(tenant: Tenant, batch: list[PendingMessage], timeout: float) -> DrainOutcome:
    """Forward a deliverable Telegram batch to the container as one OC turn.

    Singleton vs coalesced behaviour mirrors ``_drain_line_batch``; see
    that docstring. The batch claim guarantees all rows share
    ``channel_user_id`` (so all rows belong to the same Telegram chat).

    Returns ``True`` on a healthy (non-credit-limit) gateway response — see
    ``_drain_line_batch`` for the liveness-signal contract.
    """
    if not batch:
        return DrainOutcome(
            Disposition.DELIVER,
            DeliveryState.FAILED,
            gateway_responded=False,
            reason="empty_batch",
        )

    chat_id_str = batch[0].channel_user_id
    try:
        chat_id = int(chat_id_str)
    except (TypeError, ValueError):
        raise ValueError(f"telegram drain: invalid chat_id {chat_id_str!r}")

    content, user_param, user_tz = _build_batch_chat_content(batch, chat_id_str, channel="telegram")
    from apps.pii.redactor import annotate_model_context

    content = annotate_model_context(content, getattr(tenant, "pii_entity_map", None))

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
        "X-OpenClaw-Message-Channel": "telegram",
    }

    # Send a typing pulse before the slow POST so the user sees activity.
    if not suppresses_real_transport(tenant):
        _send_telegram_typing_safe(chat_id)

    resp = httpx.post(url, json=chat_payload, headers=headers, timeout=timeout)
    if _looks_like_openrouter_credit_limit(resp):
        credit_result = _handle_openrouter_credit_limit(tenant, channel="telegram", channel_user_id=str(chat_id))
        return DrainOutcome(
            Disposition.RETRY if credit_result == "ceiling_raised" else Disposition.TERMINAL,
            DeliveryState.FAILED,
            gateway_responded=False,
            reason="ceiling_raised" if credit_result == "ceiling_raised" else "budget_exhausted",
        )
    resp.raise_for_status()
    result = resp.json()

    ai_text = _extract_ai_response(result)
    if not ai_text:
        logger.warning(
            "drain_pending: empty Telegram AI response for tenant %s",
            str(tenant.id)[:8],
        )
        send_result = SendResult(DeliveryState.SENT, detail="empty_ai_text")
    elif suppresses_real_transport(tenant):
        send_result = SendResult(DeliveryState.SUPPRESSED, detail="eval_sink")
    else:
        send_result = relay_ai_response_to_telegram(tenant, chat_id, ai_text)

    _capture_conversation_turn(tenant, "telegram", chat_id_str, batch, ai_text)
    _record_usage_safe(tenant, result, message_count=len(batch))
    return _drain_outcome_from_send_result(
        send_result,
        assistant_text=ai_text or "",
        model_response_ref=_model_response_ref(result),
    )


# ---------------------------------------------------------------------------
# iOS / rich-client (app) drain — persists the reply for the client to poll
# instead of relaying to a push channel API. Routes through the tenant
# runtime like Telegram/LINE (same USER.md/memory); the ``user`` param is
# ``thread:<id>`` so each ChatThread is its own OpenClaw session.
# ---------------------------------------------------------------------------


def _drain_ios_batch(tenant: Tenant, batch: list[PendingMessage], timeout: float) -> DrainOutcome:
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
        return DrainOutcome(
            Disposition.DELIVER,
            DeliveryState.FAILED,
            gateway_responded=False,
            reason="empty_batch",
        )

    thread_id = batch[0].channel_user_id
    content, user_param, user_tz = _build_batch_chat_content(batch, thread_id, channel="ios")

    # Bridge proactive-message continuity into the iOS turn. Unlike the
    # Telegram/LINE ingress handlers (which prepend this block before enqueue),
    # the iOS ingress (``enqueue_tenant_turn``) does NOT surface it — so the
    # bridge lives HERE, at the drain, the single point where iOS content
    # becomes container-bound. Prepending once onto the assembled ``content``
    # (singleton passthrough OR coalesced rebuild) puts the block at the top of
    # the turn exactly once, never once per pending message. ``surface_proactive_
    # context`` marks the rows consumed at this same point — mirroring how the
    # other channels surface+consume where the text enters the container payload.
    # Tenant-scoped: a cron delivered over Telegram/LINE while the user has since
    # gone iOS-only still threads. Placeholder-space, like the other call sites —
    # the container operates in placeholder space, so we do NOT rehydrate here.
    from apps.router.proactive_context import surface_proactive_context

    proactive_block = surface_proactive_context(tenant=tenant)
    if proactive_block:
        content = proactive_block + content

    # Rehydrate the thread's conversation into a cold OpenClaw session. The iOS
    # session transcript lives on an ephemeral EmptyDir that is wiped on
    # hibernation/restart, so a user returning after a wake references things the
    # agent no longer remembers ("what's fable 5?"). When ``Tenant.last_wake_at``
    # is newer than the thread's last delivered turn, the session was probably
    # wiped — prepend a bounded recap of the thread's recent history so the agent
    # continues instead of greeting the user cold. Prepended AFTER the proactive
    # block so the final order is recap (oldest context) → [earlier-from-you] →
    # datetime/chat markers + user text. Exactly-once per wake and idempotent
    # across drain retries (see ``thread_recap``). The current batch's rows are
    # excluded so the recap never replays the turn being delivered.
    from apps.router.thread_recap import build_thread_recap_block

    recap_block = build_thread_recap_block(
        tenant,
        thread_id,
        exclude_client_msg_ids=_ios_client_msg_ids(batch),
    )
    if recap_block:
        content = recap_block + content

    # Last model-bound seam: recap/proactive blocks may have come from older
    # bare-placeholder rows, so refresh every entity token after assembling the
    # complete turn.
    from apps.pii.redactor import annotate_model_context

    content = annotate_model_context(content, getattr(tenant, "pii_entity_map", None))

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
        "X-OpenClaw-Message-Channel": "ios",
    }

    resp = httpx.post(url, json=chat_payload, headers=headers, timeout=timeout)
    if _looks_like_openrouter_credit_limit(resp):
        credit_result = _handle_openrouter_credit_limit(tenant, channel="ios", channel_user_id=thread_id)
        if credit_result == "ceiling_raised":
            return DrainOutcome(
                Disposition.RETRY,
                DeliveryState.FAILED,
                gateway_responded=False,
                reason="ceiling_raised",
            )
        _store_ios_turn_error(tenant, batch, "budget_exhausted")
        return DrainOutcome(
            Disposition.TERMINAL,
            DeliveryState.FAILED,
            gateway_responded=False,
            reason="budget_exhausted",
        )
    resp.raise_for_status()
    result = resp.json()

    ai_text = _extract_ai_response(result)
    _store_ios_turn_reply(tenant, batch, ai_text)
    _record_usage_safe(tenant, result, message_count=len(batch))
    # Refresh the USER.md "Conversation so far" digest so isolated proactive /
    # cron sessions (Morning Briefing, Evening Check-in, the cron heartbeat)
    # ground on this iOS turn instead of an hours-stale snapshot — the verified
    # golden-case root cause (docs/assistant-context-continuity-directive.md
    # D1 / §3 Phase 1). Telegram/LINE get this via _capture_conversation_turn →
    # record_conversation_turn; iOS content is already durable in AppChatMessage
    # (build_conversation_digest reads it directly), so ONLY the debounced push
    # was missing. Fires on a healthy gateway response, exactly where the other
    # channels capture; skipped on the credit-limit early return and on a POST
    # that raised (container down) — a stale digest must never be pushed off a
    # turn that didn't actually happen. Fail-open, off the delivery path.
    _schedule_ios_digest_refresh(tenant)
    return DrainOutcome(
        Disposition.DELIVER,
        DeliveryState.SENT,
        gateway_responded=True,
        delivered_chunks=1,
        total_chunks=1,
        assistant_text=ai_text or "",
        model_response_ref=_model_response_ref(result),
    )


def _schedule_ios_digest_refresh(tenant: Tenant) -> None:
    """Schedule the debounced USER.md conversation-digest push after a
    successful iOS drain.

    Reuses the SAME seam Telegram/LINE turns fire
    (``conversation_capture.schedule_user_md_refresh`` — a leading-edge debounced
    ``push_user_md`` through the sanitize chokepoint), so there is no parallel
    debounce. iOS turns are already durable in ``AppChatMessage`` and the digest
    reads that table directly, so — unlike Telegram/LINE — no ``ConversationTurn``
    is written here; only the previously-missing push is scheduled. Honors the
    module's threading contract (the seam itself is synchronous-on-commit under
    ``NBHD_DISABLE_BACKGROUND_THREADS``). Fail-open — a refresh hiccup must never
    fail message delivery."""
    try:
        from apps.router.conversation_capture import schedule_user_md_refresh

        schedule_user_md_refresh(tenant)
    except Exception:
        logger.warning(
            "drain_pending: ios USER.md digest refresh scheduling failed for tenant %s (non-fatal)",
            str(getattr(tenant, "id", "?"))[:8],
            exc_info=True,
        )


def _ios_client_msg_ids(batch: list[PendingMessage]) -> list[str]:
    ids = [(row.payload or {}).get("client_msg_id") for row in batch]
    return [cid for cid in ids if cid]


def _dispatch_push(target, *args) -> None:
    """Run an APNs notify helper OFF the drain path: a slow send (≤10s timeout)
    must not block the drain task / hold the per-key lease. Synchronous under
    ``NBHD_DISABLE_BACKGROUND_THREADS`` (tests) for determinism. Fail-open."""
    try:
        from django.conf import settings as _settings

        if getattr(_settings, "NBHD_DISABLE_BACKGROUND_THREADS", False):
            target(*args)
        else:
            import threading

            threading.Thread(target=target, args=args, daemon=True).start()
    except Exception:
        logger.warning("ios: push dispatch failed (non-fatal)", exc_info=True)


def _store_ios_turn_reply(tenant: Tenant, batch: list[PendingMessage], ai_text: str | None) -> None:
    """Persist the assistant reply onto the AppChatMessage rows for this
    batch so the polling client can read it. Empty / gateway-error replies
    flip the turn to ``error`` so the client doesn't poll forever."""
    from apps.router.models import AppChatMessage
    from apps.router.push_views import notify_app_reply_error, notify_app_reply_ready

    client_ids = _ios_client_msg_ids(batch)
    if not client_ids:
        return
    retried_turns = dict(
        AppChatMessage.objects.filter(
            tenant=tenant,
            client_msg_id__in=client_ids,
            status=AppChatMessage.Status.PENDING,
            retried_at__isnull=False,
        ).values_list("client_msg_id", "id")
    )
    retried_client_ids = list(retried_turns)
    now = timezone.now()
    if ai_text:
        # A coalesced batch (N>1) yields ONE combined reply. Attach it to a single
        # representative row (the last message in the batch) so the since-feed,
        # thread history, and the USER.md digest each emit exactly one assistant
        # row. Writing `text` onto every row instead fans the same reply out N
        # times across all three surfaces (3 quick messages → the same assistant
        # bubble repeated 3×). The other rows still flip to a terminal READY state
        # (empty reply_text) so the polling client stops waiting on them — and
        # `_app_rows` / the digest both suppress an assistant row when reply_text
        # is empty, so no duplicate is rendered.
        rep_id = client_ids[-1]
        with transaction.atomic():
            text, push_text, reply_redactions, quick_replies, journal_link = _clean_assistant_text_for_app(
                tenant,
                ai_text,
                artifact_dedup_key=rep_id,
            )
            # Clear partial_text: the final reply supersedes any pseudo-streamed
            # text-so-far, and the row is leaving 'pending' where the partial is
            # meaningful.
            AppChatMessage.objects.filter(tenant=tenant, client_msg_id=rep_id).update(
                reply_text=text,
                status=AppChatMessage.Status.READY,
                replied_at=now,
                partial_text="",
                # Per-turn transparency metadata rides the SAME representative row as
                # reply_text (siblings stay null — see below); null when the reply
                # obfuscated nothing.
                reply_redactions=reply_redactions or None,
                # Quick-reply button labels ride the same representative row, for
                # the same reason. null when the reply carried no marker.
                quick_replies=quick_replies or None,
                # The "View in Journal" deep-link rides the same representative row,
                # for the same reason. null when the reply carried no marker.
                journal_link=journal_link or None,
            )
            other_ids = [cid for cid in client_ids if cid != rep_id]
            if other_ids:
                AppChatMessage.objects.filter(tenant=tenant, client_msg_id__in=other_ids).update(
                    reply_text="",
                    status=AppChatMessage.Status.READY,
                    replied_at=now,
                    partial_text="",
                )
        # Notify the device the reply landed (closes the fire-and-forget gap for
        # Siri-escalated / backgrounded turns). No-op unless APNs is configured;
        # fail-open; idempotent (notified_at claim). The app suppresses the alert
        # if the thread is foregrounded. Push only carries the representative id so
        # the device shows one "reply ready" notification, not N. The lock-screen
        # body uses the REHYDRATED copy (``push_text``) — ``reply_text`` is stored
        # placeholder-space, so pushing ``text`` would leak a raw ``[PERSON_1]``.
        _dispatch_push(notify_app_reply_ready, tenant, [rep_id], push_text)
        for client_msg_id in retried_client_ids:
            logger.info(
                "retry_succeeded count=1 tenant=%s turn=%s",
                str(tenant.id)[:8],
                str(retried_turns[client_msg_id])[:16],
            )
    else:
        AppChatMessage.objects.filter(tenant=tenant, client_msg_id__in=client_ids).update(
            status=AppChatMessage.Status.ERROR,
            error="empty_response",
            replied_at=now,
            partial_text="",
        )
        # An empty terminal reply is still a turn the user is waiting on — push a
        # generic 'couldn't finish' so a backgrounded / Siri-escalated turn
        # doesn't go silent until the next foreground.
        ordinary_client_ids = [client_msg_id for client_msg_id in client_ids if client_msg_id not in retried_client_ids]
        if ordinary_client_ids:
            _dispatch_push(notify_app_reply_error, tenant, ordinary_client_ids)
        for client_msg_id in retried_client_ids:
            _notify_retry_exhausted(tenant, retried_turns[client_msg_id], client_msg_id, "empty_response")


def _store_ios_turn_error(tenant: Tenant, batch: list[PendingMessage], reason: str) -> None:
    client_ids = _ios_client_msg_ids(batch)
    if not client_ids:
        return
    with transaction.atomic():
        _terminalize_pending_app_turns(
            tenant,
            client_ids,
            reason,
            now=timezone.now(),
        )


def _clean_assistant_text_for_app(
    tenant: Tenant,
    ai_text: str,
    *,
    artifact_dedup_key: str,
) -> tuple[str, str, list[dict], list[str] | None, dict | None]:
    """Split one agent reply into its at-rest form, its lock-screen form, its
    PII-transparency metadata, any quick-reply button labels, and any journal
    deep-link.

    Returns ``(stored_text, push_text, reply_redactions, quick_replies, journal_link)``:

    * ``stored_text`` — PLACEHOLDER-SPACE (``[PERSON_1]`` kept verbatim), agent
      markers stripped. This is what lands in ``AppChatMessage.reply_text``
      (pseudonymize-at-rest): the owner-facing read seams (``_serialize_message``
      and the ``?since=`` feed's ``_app_rows``) rehydrate it on the way out, so
      real names are never stored in this column.
    * ``push_text`` — rehydrated, real-value text for the APNs lock-screen body
      (owner-facing, so it must NOT show a raw placeholder).
    * ``reply_redactions`` — the placeholders the assistant emitted, resolved to
      the real values they stand for, captured from the placeholder-space text
      (see :func:`placeholder_redactions`).
    * ``quick_replies`` — up to 3 tappable choice labels parsed from a trailing
      ``[[quick-replies: A | B | C]]`` marker (see
      ``apps.router.quick_replies.extract_quick_replies``), or ``None``.
    * ``journal_link`` — a ``{"kind", "slug", "title"}`` deep-link parsed from a
      standalone ``[[journal-link: kind|slug|title]]`` line (see
      ``apps.router.journal_link.extract_journal_link``), or ``None``. ``title``
      is captured placeholder-space and rehydrated at the owner-facing seams.

    Insight recording consumes the same placeholder-space copy as ``stored_text``;
    owner insight reads rehydrate at their response boundary. Mirrors the relevant
    parts of ``relay_ai_response_to_telegram``."""
    from apps.insights.markers import INSIGHT_MARKER_RE
    from apps.pii.egress import redact_known_values
    from apps.router.journal_link import extract_journal_link
    from apps.router.quick_replies import extract_quick_replies

    # Re-establish placeholder-space before any persistence-derived metadata or
    # marker extraction. This catches a model echo of a value it learned from a
    # contaminated local tool result without minting a new entity.
    ai_text = redact_known_values(tenant, ai_text, seam="ios_reply_storage")
    entity_map = getattr(tenant, "pii_entity_map", None)
    reply_redactions = placeholder_redactions(ai_text, entity_map)

    # Parse the trailing markers off the guarded reply so quick-reply labels and
    # journal-link titles remain placeholder-space at rest too.
    # step (insight extraction, chart/MEDIA stripping, rehydration) operates on
    # text that's already marker-free. Quick-replies stays first so its existing
    # final-line behavior is unchanged; journal-link then scans every remaining
    # line, including when prose follows it.
    ai_text, quick_replies = extract_quick_replies(ai_text, tenant_id=tenant.id, channel="ios")
    ai_text, journal_link = extract_journal_link(ai_text, tenant_id=tenant.id, channel="ios")

    # Record insights from the guarded placeholder-space reply. Rehydration is
    # owner-presentation behavior and must not run before AssistantInsight writes.
    try:
        from apps.insights.markers import extract_and_record_insights

        extract_and_record_insights(ai_text, tenant=tenant)
    except Exception:
        logger.exception("insight marker extraction failed (ios drain)")

    # Stored copy stays placeholder-space: strip the SAME insight markers WITHOUT
    # re-recording (already recorded above) — keeping just the visible statement,
    # exactly as ``extract_and_record_insights`` leaves it — plus chart / MEDIA.
    stored_text = INSIGHT_MARKER_RE.sub(lambda m: (m.group(2) or "").strip(), ai_text)
    stored_text = re.sub(r"\[\[chart:\w+(?:\|.+?)?\]\]", "", stored_text)
    stored_text = re.sub(r"MEDIA:\S+", "", stored_text)

    from apps.router.structured_artifacts import externalize_large_structured_reply

    externalized = externalize_large_structured_reply(
        tenant=tenant,
        text=stored_text,
        source="ios",
        dedup_key=artifact_dedup_key,
        journal_link=journal_link,
    )
    stored_text = externalized.stored_text
    journal_link = externalized.journal_link

    # Guard only after span-based externalization; placeholder expansion must
    # never invalidate the detector's offsets.
    from apps.router.reply_text import finalize_outbound_text

    push_text = finalize_outbound_text(stored_text, entity_map, tenant_id=tenant.id, channel="ios_push")
    stored_text = clamp_reply_text(stored_text.strip())
    push_text = clamp_reply_text(push_text.strip())

    return stored_text, push_text, reply_redactions, quick_replies, journal_link


# ---------------------------------------------------------------------------
# Telegram response relay (mirrors ``relay_ai_response_to_line`` in
# line_webhook.py — kept here so the queue can deliver Telegram replies
# from the Django web worker without having to reach into the long-lived
# poller process).
# ---------------------------------------------------------------------------


def relay_ai_response_to_telegram(tenant: Tenant, chat_id: int, ai_text: str) -> SendResult:
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
        return SendResult(DeliveryState.FAILED, detail="no_text")
    if suppresses_real_transport(tenant):
        logger.error(
            "eval-sink transport block: tenant=%s transport=telegram",
            tenant.id,
        )
        return SendResult(DeliveryState.SUPPRESSED, detail="eval_sink")

    # Quick-reply buttons and the journal deep-link chip are iOS-only for now —
    # Telegram has no transport for either here, so just strip the markers
    # (never let them leak as raw text).
    from apps.router.journal_link import extract_journal_link
    from apps.router.quick_replies import extract_quick_replies

    ai_text, _quick_replies = extract_quick_replies(ai_text, tenant_id=tenant.id, channel="telegram_drain")
    ai_text, _journal_link = extract_journal_link(ai_text, tenant_id=tenant.id, channel="telegram_drain")

    # Final owner-facing integrity guard.
    entity_map = getattr(tenant, "pii_entity_map", None)
    from apps.router.reply_text import finalize_outbound_text

    ai_text = finalize_outbound_text(ai_text, entity_map, tenant_id=tenant.id, channel="telegram_drain")

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
    # via sendPhoto. Mirrors ``poller._send_rich_response``.
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

    # Parse inline buttons: [[button:Label|callback_data]] — same as
    # ``poller._send_rich_response``. Without this the markers leaked as
    # literal text on the (production) queue drain path and the
    # ``agent:``-prefixed callback at poller.py was never reachable on
    # Telegram. The poller already handles the resulting button taps.
    button_pattern = re.compile(r"\[\[button:([^|]+)\|([^\]]+)\]\]")
    buttons = button_pattern.findall(text)
    text = button_pattern.sub("", text).strip()

    reply_markup = None
    if buttons:
        keyboard = [[{"text": label.strip(), "callback_data": f"agent:{data.strip()}"}] for label, data in buttons]
        reply_markup = {"inline_keyboard": keyboard}

    if not text:
        # Buttons-only reply: agent emitted [[button:...]] markers with no prose.
        # text is empty after stripping but we still need to deliver the keyboard.
        # Telegram's sendMessage requires non-empty text; use a middle-dot placeholder
        # (printable, survives strip()) so reply_markup is not silently dropped.
        if reply_markup:
            return _send_telegram_html_chunks(chat_id, "·", reply_markup=reply_markup)
        return SendResult(DeliveryState.SENT, detail="markers_only")

    return _send_telegram_html_chunks(chat_id, text, reply_markup=reply_markup)


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
    if blocks_real_transport_for_identifier("telegram", chat_id):
        return
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


def _send_telegram_html_chunks(chat_id: int, text: str, reply_markup: dict | None = None) -> SendResult:
    """Render the assistant's markdown to Telegram HTML and deliver it.

    The agent emits CommonMark/GFM; Telegram's legacy ``Markdown`` parse-mode
    leaks ``##`` headings, ``---`` rules, ``**bold**`` and tables literally, so
    we render to Telegram's HTML subset (``apps.router.telegram_format``) —
    bold headings, aligned monospace tables, real anchors, no visible markdown.
    Each block-bounded chunk is sent with ``parse_mode="HTML"``; on the rare
    rejection we degrade that chunk to tag-free text (still markdown-free).

    ``reply_markup`` (e.g. an ``inline_keyboard`` of agent buttons) is attached
    to the LAST chunk only, mirroring ``TelegramPoller._send_rich_response``.
    """
    if blocks_real_transport_for_identifier("telegram", chat_id):
        return SendResult(DeliveryState.SUPPRESSED, detail="eval_sink")
    base = _telegram_api_base()
    if not base:
        logger.warning("drain_pending: cannot send telegram message — no bot token")
        return SendResult(DeliveryState.FAILED, detail="no_bot_token")

    from apps.router.telegram_format import render_telegram_html, strip_telegram_html

    chunks = render_telegram_html(text)
    if not chunks:
        return SendResult(DeliveryState.FAILED, detail="empty_render")

    delivered_chunks = 0
    ambiguous = False
    for i, chunk in enumerate(chunks):
        if i > 0:
            time.sleep(0.3)  # brief delay between chunks (matches poller)
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        try:
            resp = httpx.post(
                f"{base}/sendMessage",
                json=payload,
                timeout=10,
            )
            if resp.is_success:
                delivered_chunks += 1
                continue
            if resp.status_code == 400:
                # HTML rejected — retry as tag-free plain text (no markdown).
                plain_payload = {"chat_id": chat_id, "text": strip_telegram_html(chunk)}
                if reply_markup and i == len(chunks) - 1:
                    plain_payload["reply_markup"] = reply_markup
                plain = httpx.post(
                    f"{base}/sendMessage",
                    json=plain_payload,
                    timeout=10,
                )
                if plain.is_success:
                    delivered_chunks += 1
                continue
            logger.warning(
                "drain_pending: sendMessage failed (%s): %s",
                resp.status_code,
                resp.text[:200],
            )
        except Exception:
            logger.exception("drain_pending: sendMessage exception")
            ambiguous = True

    total_chunks = len(chunks)
    if ambiguous:
        state = DeliveryState.AMBIGUOUS
    elif delivered_chunks == total_chunks:
        state = DeliveryState.SENT
    elif delivered_chunks:
        state = DeliveryState.PARTIAL
    else:
        state = DeliveryState.FAILED
    return SendResult(
        state,
        delivered_chunks=delivered_chunks,
        total_chunks=total_chunks,
    )


def _send_telegram_markdown(chat_id: int, text: str, reply_markup: dict | None = None) -> bool:
    return bool(_send_telegram_html_chunks(chat_id, text, reply_markup=reply_markup))


def _send_telegram_photo(chat_id: int, photo_path: str, tenant: Tenant) -> bool:
    """Download a photo from the tenant's workspace file share and send
    it to the Telegram chat.

    Mirrors ``TelegramPoller._send_photo`` — kept here so the drain task
    doesn't need to reach into the poller process.
    """
    if suppresses_real_transport(tenant):
        logger.error(
            "eval-sink transport block: tenant=%s transport=telegram",
            tenant.id,
        )
        return False
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


def _model_response_ref(result: Any) -> str:
    """Extract the gateway response id without assuming a non-dict shape."""
    if not isinstance(result, dict):
        return ""
    value = result.get("id", "")
    return str(value) if value is not None else ""


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


def reap_stale_app_chat_messages_task() -> dict:
    """Terminalize old tenant-runtime app turns orphaned before enqueue.

    The normal queue drain/reaper retains ownership whenever a correlated
    PENDING PendingMessage still exists, regardless of its lease. This sweep
    covers only the creation-time gap where AppChatMessage was committed but
    its queue row was never created. Work is ordered and capped so a backlog
    cannot monopolize the five-minute cron tick.
    """
    now = timezone.now()
    cutoff = now - timedelta(seconds=_STALE_APP_CHAT_AGE_SECONDS)
    active_queue_row = (
        PendingMessage.objects.annotate(
            payload_client_msg_id=KeyTextTransform("client_msg_id", "payload"),
        )
        .filter(
            tenant_id=models.OuterRef("tenant_id"),
            delivery_status=PendingMessage.Status.PENDING,
            payload_client_msg_id=models.OuterRef("client_msg_id"),
        )
        .order_by()
    )

    with transaction.atomic():
        stale_rows = list(
            AppChatMessage.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("tenant")
            .annotate(has_pending_queue=models.Exists(active_queue_row))
            .filter(
                status=AppChatMessage.Status.PENDING,
                source=AppChatMessage.Source.TENANT,
                created_at__lt=cutoff,
                has_pending_queue=False,
            )
            .order_by("created_at", "id")[:_STALE_APP_CHAT_BATCH_LIMIT]
        )
        if not stale_rows:
            return {"reaped": 0}

        stale_ids = [row.id for row in stale_rows]
        AppChatMessage.objects.filter(
            id__in=stale_ids,
            status=AppChatMessage.Status.PENDING,
        ).update(
            status=AppChatMessage.Status.ERROR,
            error="stale",
            replied_at=now,
            partial_text="",
        )

        by_tenant: dict[str, tuple[Tenant, list[str]]] = {}
        for row in stale_rows:
            tenant_key = str(row.tenant_id)
            if tenant_key not in by_tenant:
                by_tenant[tenant_key] = (row.tenant, [])
            by_tenant[tenant_key][1].append(row.client_msg_id)

        from apps.router.push_views import notify_app_reply_error

        for tenant, client_msg_ids in by_tenant.values():
            transaction.on_commit(
                lambda tenant=tenant, ids=list(client_msg_ids): _dispatch_push(
                    notify_app_reply_error,
                    tenant,
                    ids,
                )
            )

    return {"reaped": len(stale_rows)}


# ---------------------------------------------------------------------------
# Retention sweeper — bounds how long terminal queue rows (which still hold
# redacted user text) live. DELIVERED rows are hard-deleted the instant the
# drain completes; this daily cron is the residual backstop.
# ---------------------------------------------------------------------------


def cleanup_stale_pending_messages_task() -> dict:
    """Delete terminal ``PendingMessage`` rows (FAILED + any residual
    DELIVERED) older than 14 days.

    ``PendingMessage`` is a transient per-tenant forwarding queue. DELIVERED
    rows are now hard-deleted the instant the drain completes (see
    ``drain_pending_messages_for_tenant_task``); this cron is the residual
    sweeper for the two cases that still leave a terminal row behind:

      - FAILED rows, deliberately retained short-term for apology / debug
        (past-cap drop, stale-age drop, orphaned-tenant cleanup). Both
        ``payload`` and ``user_text`` hold (redacted) user message content.
      - DELIVERED rows whose post-drain hard-delete didn't land (a worker
        crash between the terminal-state commit and the delete).

    PENDING rows are never touched here — the drain + reaper own their
    lifecycle; a genuinely stuck PENDING row is flipped to FAILED with an
    apology on the next drain tick once it crosses the staleness threshold,
    then swept here 14 days later. This bounds the queue's retention instead of
    letting it stay a permanent store of user text
    (docs/encryption-at-rest-directive.md §7, Phase 0 PR-3). Mirrors
    ``cleanup_delivered_buffers_task``.
    """
    cutoff = timezone.now() - timedelta(days=14)
    deleted, _ = PendingMessage.objects.filter(
        delivery_status__in=[
            PendingMessage.Status.FAILED,
            PendingMessage.Status.DELIVERED,
        ],
        created_at__lt=cutoff,
    ).delete()

    logger.info("cleanup_stale_pending_messages: deleted %d old terminal rows", deleted)
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Misc — kept so callers can import this module without importing every
# helper individually.
# ---------------------------------------------------------------------------

# Some surface used by tests / callers — keep imports stable.
__all__ = [
    "PendingMessage",
    "cleanup_stale_pending_messages_task",
    "drain_pending_messages_for_tenant_task",
    "dropped_retry_dedup_id",
    "dropped_retry_health_dedup_id",
    "enqueue_message_for_tenant",
    "reap_stuck_inbound_messages_task",
    "relay_ai_response_to_telegram",
    "retry_dropped_app_turn_task",
]
