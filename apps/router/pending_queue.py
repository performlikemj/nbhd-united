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

from apps.billing.services import record_usage
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

    The drain task is published with ``retries=1`` (vs QStash's default
    of 3) because the task already has application-level resilience: the
    in-flight lease prevents duplicate POSTs, the per-message attempt
    cap prevents wedged rows from blocking the queue forever, and the
    drain re-schedules itself when more rows remain. Letting QStash
    retry 3x would just spawn extra drain attempts that observe the
    live lease and bail (cheap but noisy).

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
        user_text=(user_text_excerpt or "")[:200],
    )

    try:
        from apps.cron.publish import publish_task

        publish_task(
            "drain_pending_messages_for_tenant",
            str(tenant.id),
            channel,
            channel_user_id or "",
            retries=1,
        )
    except Exception:
        logger.exception(
            "pending_queue: failed to publish drain task for tenant %s — "
            "row %s will sit until the next drain tick reaches it",
            str(tenant.id)[:8],
            msg.id,
        )

    return msg


# ---------------------------------------------------------------------------
# Internal: claim + drain
# ---------------------------------------------------------------------------


def _claim_next_pending_message(
    tenant: Tenant,
    channel: str,
    channel_user_id: str,
    timeout_seconds: float,
) -> PendingMessage | None:
    """Claim the next deliverable pending row for the given key.

    Returns the claimed row (with ``delivery_in_flight_until`` extended)
    or ``None`` if no row is available — either the queue is empty for
    this key or every undelivered row currently has a live lease held
    by a concurrent drain task.

    The claim runs inside a ``SELECT ... FOR UPDATE SKIP LOCKED``
    transaction so two concurrent drain invocations (e.g. the row's
    initial scheduled drain and a webhook-triggered drain for the next
    row) can't both grab the same row and fire overlapping POSTs at the
    container — the exact failure mode the OpenClaw claude-cli backend
    rejects with "Claude CLI live session is already handling a turn".
    """
    lease_seconds = timeout_seconds * _IN_FLIGHT_LEASE_FACTOR

    with transaction.atomic():
        now = timezone.now()
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
        msg = qs.first()
        if not msg:
            return None

        # Past the cap → return without taking a lease. The drain loop
        # handles dropping it (no network call needed).
        if msg.delivery_attempts >= _MAX_DELIVERY_ATTEMPTS:
            return msg

        msg.delivery_in_flight_until = now + timedelta(seconds=lease_seconds)
        msg.save(update_fields=["delivery_in_flight_until"])
        return msg


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
    msg = _claim_next_pending_message(tenant, channel, channel_user_id or "", chat_timeout)

    if msg is None:
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

    # Past the cap → drop and surface (no lease was taken inside _claim).
    if msg.delivery_attempts >= _MAX_DELIVERY_ATTEMPTS:
        logger.warning(
            "drain_pending: dropping msg %s for tenant %s after %d attempts",
            msg.id,
            tenant_id[:8],
            msg.delivery_attempts,
        )
        msg.delivery_status = PendingMessage.Status.FAILED
        msg.delivered_at = timezone.now()
        msg.delivery_in_flight_until = None
        msg.save(
            update_fields=[
                "delivery_status",
                "delivered_at",
                "delivery_in_flight_until",
            ]
        )
        _send_apology_for_dropped_pending_message(tenant, msg)
        # Schedule another drain in case the next message is deliverable.
        if _has_more_pending(tenant, channel, channel_user_id or ""):
            _reschedule_drain(tenant, channel, channel_user_id or "")
        return {"delivered": 0, "failed": 0, "dropped": 1, "skipped_in_flight": 0}

    delivered = 0
    failed = 0
    try:
        if channel == PendingMessage.Channel.LINE:
            _drain_line_message(tenant, msg, chat_timeout)
        elif channel == PendingMessage.Channel.TELEGRAM:
            _drain_telegram_message(tenant, msg, chat_timeout)
        else:
            raise ValueError(f"Unknown channel: {channel!r}")

        msg.delivery_status = PendingMessage.Status.DELIVERED
        msg.delivered_at = timezone.now()
        msg.delivery_in_flight_until = None
        msg.save(
            update_fields=[
                "delivery_status",
                "delivered_at",
                "delivery_in_flight_until",
            ]
        )
        delivered = 1

    except Exception:
        logger.exception(
            "drain_pending: failed to deliver msg %s for tenant %s (attempt %d/%d)",
            msg.id,
            tenant_id[:8],
            msg.delivery_attempts + 1,
            _MAX_DELIVERY_ATTEMPTS,
        )
        msg.delivery_attempts += 1
        msg.delivery_in_flight_until = None
        msg.save(
            update_fields=[
                "delivery_attempts",
                "delivery_in_flight_until",
            ]
        )
        failed = 1

    # On success: if more pending rows remain for this key, schedule the
    # next drain immediately so back-to-back messages keep flowing.
    #
    # On failure we deliberately do NOT re-schedule. The QStash retry
    # (``retries=1`` set when the drain was first published) handles the
    # second-chance attempt with QStash's natural backoff, and the
    # per-message ``delivery_attempts`` counter still caps total attempts
    # at ``_MAX_DELIVERY_ATTEMPTS``. Re-scheduling here would synchronously
    # cascade through the cap in tests and burn the attempts budget on a
    # request that's almost certainly going to keep failing.
    if delivered and _has_more_pending(tenant, channel, channel_user_id or ""):
        _reschedule_drain(tenant, channel, channel_user_id or "")

    if failed:
        # Surface a non-2xx so QStash retries the task once. The
        # application-level lease + attempt cap prevents this from
        # spawning a duplicate POST against the container.
        raise RuntimeError(
            f"drain_pending: msg {msg.id} for tenant {tenant_id[:8]} failed "
            f"(attempt {msg.delivery_attempts}/{_MAX_DELIVERY_ATTEMPTS})"
        )

    return {
        "delivered": delivered,
        "failed": failed,
        "dropped": 0,
        "skipped_in_flight": 0,
    }


def _reschedule_drain(tenant: Tenant, channel: str, channel_user_id: str) -> None:
    """Schedule another drain pass for the same key.

    Called when (a) we just delivered a row and more remain, or (b) we
    just dropped a maxed-out row at the head of the queue and want to
    immediately try the next one.
    """
    try:
        from apps.cron.publish import publish_task

        publish_task(
            "drain_pending_messages_for_tenant",
            str(tenant.id),
            channel,
            channel_user_id or "",
            retries=1,
        )
    except Exception:
        logger.exception(
            "drain_pending: failed to re-schedule drain for tenant %s key=%s/%s",
            str(tenant.id)[:8],
            channel,
            (channel_user_id or "")[:24],
        )


# ---------------------------------------------------------------------------
# Apology for messages dropped past the attempts cap
# ---------------------------------------------------------------------------


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
    from apps.router.error_messages import error_msg

    excerpt = (msg.user_text or "").strip().replace("\n", " ")
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


def _drain_line_message(tenant: Tenant, msg: PendingMessage, timeout: float) -> None:
    """Forward one LINE message to the container and relay the reply back."""
    from apps.router.line_webhook import relay_ai_response_to_line

    payload = msg.payload or {}
    line_user_id = msg.channel_user_id
    message_text = payload.get("message_text") or ""
    user_param = payload.get("user_param") or line_user_id
    user_tz = payload.get("user_timezone") or "UTC"
    # ``reply_token`` is intentionally NOT used: by the time the queue
    # drains, the LINE Reply API window (~1 min) is almost always
    # closed. We always Push.

    url = f"https://{tenant.container_fqdn}/v1/chat/completions"
    gateway_token = getattr(settings, "NBHD_INTERNAL_API_KEY", "").strip()

    chat_payload = {
        "model": "openclaw",
        "messages": [{"role": "user", "content": message_text}],
        "user": user_param,
    }
    headers = {
        "Authorization": f"Bearer {gateway_token}",
        "X-User-Timezone": user_tz,
        "X-Line-User-Id": line_user_id,
        "X-Channel": "line",
    }

    resp = httpx.post(url, json=chat_payload, headers=headers, timeout=timeout)
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

    _record_usage_safe(tenant, result)


def _drain_telegram_message(tenant: Tenant, msg: PendingMessage, timeout: float) -> None:
    """Forward one Telegram message to the container and relay the reply back."""
    payload = msg.payload or {}
    chat_id_str = msg.channel_user_id
    try:
        chat_id = int(chat_id_str)
    except (TypeError, ValueError):
        raise ValueError(f"telegram drain: invalid chat_id {chat_id_str!r}")

    message_text = payload.get("message_text") or ""
    user_param = payload.get("user_param") or chat_id_str
    user_tz = payload.get("user_timezone") or "UTC"

    url = f"https://{tenant.container_fqdn}/v1/chat/completions"
    gateway_token = getattr(settings, "NBHD_INTERNAL_API_KEY", "").strip()

    chat_payload = {
        "model": "openclaw",
        "messages": [{"role": "user", "content": message_text}],
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
    resp.raise_for_status()
    result = resp.json()

    ai_text = _extract_ai_response(result)
    if ai_text:
        relay_ai_response_to_telegram(tenant, chat_id, ai_text)

    _record_usage_safe(tenant, result)


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
    """Send a (potentially long) message to Telegram with Markdown,
    falling back to plain text per chunk on parse-mode rejection."""
    base = _telegram_api_base()
    if not base:
        logger.warning("drain_pending: cannot send telegram message — no bot token")
        return False

    chunks = _split_telegram_message(text)
    overall = True
    for i, chunk in enumerate(chunks):
        if i > 0:
            time.sleep(0.3)  # brief delay between chunks (matches poller)
        try:
            resp = httpx.post(
                f"{base}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
                timeout=10,
            )
            if resp.is_success:
                continue
            if resp.status_code == 400:
                # Markdown parse error — retry as plain text.
                plain = httpx.post(
                    f"{base}/sendMessage",
                    json={"chat_id": chat_id, "text": chunk},
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


def _record_usage_safe(tenant: Tenant, result: Any) -> None:
    """Record token usage from a chat-completions response. Swallows
    errors so a billing failure can never wedge the queue."""
    if not isinstance(result, dict):
        return
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return

    input_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
    model_used = result.get("model", "") or ""

    if not (input_tokens or output_tokens):
        return

    try:
        record_usage(
            tenant=tenant,
            event_type="message",
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            model_used=model_used,
        )
    except Exception:
        logger.exception("drain_pending: failed to record usage for tenant %s", tenant.id)


# ---------------------------------------------------------------------------
# Misc — kept so callers can import this module without importing every
# helper individually.
# ---------------------------------------------------------------------------

# Some surface used by tests / callers — keep imports stable.
__all__ = [
    "PendingMessage",
    "drain_pending_messages_for_tenant_task",
    "enqueue_message_for_tenant",
    "relay_ai_response_to_telegram",
]
