"""LINE Messaging API webhook receiver.

POST /api/v1/line/webhook/

- Verifies LINE signature (HMAC-SHA256)
- Parses events from the request body
- Handles follow, message (text, audio/voice), and unfollow events
- Processes AI forwarding asynchronously (LINE requires 200 within 1 second)
- Uses LINE Push Message API for responses (reply_token expires too fast)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading

import httpx
from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.billing.services import (
    check_budget,
    record_usage,
    resolve_model_for_attribution,
)
from apps.router.error_messages import error_msg
from apps.router.line_flex import (
    attach_quick_reply,
    build_flex_bubble,
    build_short_bubble,
    build_status_bubble,
    extract_quick_reply_buttons,
    telegram_keyboard_to_quick_reply,
)
from apps.tenants.models import Tenant, User

logger = logging.getLogger(__name__)

LINE_API_BASE = "https://api.line.me/v2/bot"
LINE_CONTENT_API = "https://api-data.line.me/v2/bot/message"
VOICE_CHAT_TIMEOUT_EXTRA = 60.0  # additional seconds for voice (Whisper transcription)
LOADING_SECONDS = 60  # loading animation max (auto-clears on response)
WHISPER_API_URL = "https://api.openai.com/v1/audio/transcriptions"


def _get_channel_secret() -> str:
    return getattr(settings, "LINE_CHANNEL_SECRET", "").strip()


def _get_access_token() -> str:
    return getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "").strip()


def _verify_signature(body: bytes, signature: str) -> bool:
    """Verify LINE webhook signature using HMAC-SHA256."""
    channel_secret = _get_channel_secret()
    if not channel_secret:
        logger.error("LINE_CHANNEL_SECRET not configured")
        return False

    mac = hmac.new(
        channel_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    )
    expected = base64.b64encode(mac.digest()).decode("utf-8")
    return hmac.compare_digest(signature, expected)


def _show_loading(line_user_id: str) -> None:
    """Show typing/loading animation in LINE chat. Fire-and-forget."""
    access_token = _get_access_token()
    if not access_token:
        return
    try:
        httpx.post(
            f"{LINE_API_BASE}/chat/loading/start",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"chatId": line_user_id, "loadingSeconds": LOADING_SECONDS},
            timeout=3,
        )
    except Exception:
        pass  # non-critical — don't log noise


def _transcribe_line_audio(message_id: str) -> str | None:
    """Download audio from LINE Content API and transcribe via OpenAI Whisper.

    Returns transcribed text, or None on failure.
    """
    openai_key = getattr(settings, "OPENAI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        logger.warning("Cannot transcribe LINE audio: no OPENAI_API_KEY configured")
        return None

    access_token = _get_access_token()
    if not access_token:
        logger.warning("Cannot transcribe LINE audio: no LINE_CHANNEL_ACCESS_TOKEN")
        return None

    try:
        # 1. Download audio content from LINE
        dl_resp = httpx.get(
            f"{LINE_CONTENT_API}/{message_id}/content",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if not dl_resp.is_success:
            logger.warning(
                "Failed to download LINE audio %s: %s",
                message_id,
                dl_resp.status_code,
            )
            return None

        audio_data = dl_resp.content
        if not audio_data:
            logger.warning("LINE audio content empty for message %s", message_id)
            return None

        # LINE voice messages are m4a; determine from content-type header
        content_type = dl_resp.headers.get("content-type", "")
        if "m4a" in content_type or "mp4" in content_type:
            ext = "m4a"
        elif "ogg" in content_type:
            ext = "ogg"
        elif "wav" in content_type:
            ext = "wav"
        else:
            ext = "m4a"  # LINE default for voice messages

        # 2. Transcribe via OpenAI Whisper API
        whisper_resp = httpx.post(
            WHISPER_API_URL,
            headers={"Authorization": f"Bearer {openai_key}"},
            files={"file": (f"voice.{ext}", audio_data, f"audio/{ext}")},
            data={"model": "whisper-1"},
            timeout=30,
        )
        if not whisper_resp.is_success:
            logger.warning(
                "Whisper transcription failed for LINE audio %s: %s %s",
                message_id,
                whisper_resp.status_code,
                whisper_resp.text[:200],
            )
            return None

        text = whisper_resp.json().get("text", "").strip()
        if text:
            logger.info(
                "Transcribed LINE audio %s (%d bytes → %d chars)",
                message_id,
                len(audio_data),
                len(text),
            )
            return text

        return None

    except Exception:
        logger.exception("LINE audio transcription error for message %s", message_id)
        return None


def _post_line_reply(reply_token: str, messages: list[dict]) -> dict | None:
    """POST to LINE Reply API. Returns parsed response body on success
    (which contains ``sentMessages``), else ``None``.
    """
    access_token = _get_access_token()
    if not access_token or not reply_token:
        return None
    try:
        resp = httpx.post(
            f"{LINE_API_BASE}/message/reply",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"replyToken": reply_token, "messages": messages},
            timeout=10,
        )
        if resp.is_success:
            try:
                return resp.json()
            except Exception:
                return {}
        logger.debug("LINE reply failed (%s): %s", resp.status_code, resp.text[:200])
        return None
    except Exception:
        logger.debug("LINE reply exception", exc_info=True)
        return None


def _send_line_reply(reply_token: str, messages: list[dict]) -> bool:
    """Send messages via LINE Reply Message API (free, unlimited).

    Returns True on success, False if token expired or other failure.
    """
    return _post_line_reply(reply_token, messages) is not None


def _send_line_messages(
    line_user_id: str,
    messages: list[dict],
    reply_token: str | None = None,
    tenant=None,
) -> bool:
    """Send messages, preferring Reply API (free) with Push fallback.

    Tries reply_token first. If it fails (expired/missing), falls back to Push.

    When ``tenant`` is provided, the ``sentMessages`` IDs returned by LINE
    are persisted via ``_record_line_outbound`` so future quote-replies
    can be resolved to their excerpt. Recording is best-effort and never
    breaks the send path; omit ``tenant`` to skip it.
    """
    data = _post_line_messages(line_user_id, messages, reply_token=reply_token)
    if data is None:
        return False
    if tenant is not None:
        _record_line_outbound(tenant, line_user_id, data.get("sentMessages") or [], messages)
    return True


def _post_line_messages(
    line_user_id: str,
    messages: list[dict],
    reply_token: str | None = None,
) -> dict | None:
    """Send via Reply API (free) with Push fallback, returning the LINE
    response body (containing ``sentMessages``) so callers can record IDs.

    Returns ``None`` only when both paths fail.
    """
    if reply_token:
        data = _post_line_reply(reply_token, messages)
        if data is not None:
            return data
    return _post_line_push(line_user_id, messages)


def _post_line_push(line_user_id: str, messages: list[dict]) -> dict | None:
    """POST to LINE Push API. Returns parsed response body on success
    (which contains ``sentMessages``), else ``None``.
    """
    access_token = _get_access_token()
    if not access_token:
        logger.error("LINE_CHANNEL_ACCESS_TOKEN not configured")
        return None

    try:
        resp = httpx.post(
            f"{LINE_API_BASE}/message/push",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "to": line_user_id,
                "messages": messages,
            },
            timeout=10,
        )
        if not resp.is_success:
            logger.warning(
                "LINE push message failed (%s): %s",
                resp.status_code,
                resp.text[:300],
            )
            _maybe_trip_monthly_quota(resp.status_code, resp.text)
            return None
        try:
            return resp.json()
        except Exception:
            return {}
    except Exception:
        logger.exception("Failed to send LINE push message to %s", line_user_id)
        return None


def _maybe_trip_monthly_quota(status_code: int, body: str) -> None:
    """If a Push response indicates monthly-cap exhaustion, flip the
    fleet-wide quota state and enqueue the user-facing fan-out handler.
    Safe to call on every non-success — the helper short-circuits when
    the body isn't the monthly-limit signal.

    The fan-out is dispatched via QStash (out-of-band) rather than
    inline so we don't block the user-facing send path on N email
    sends + N user-row writes. The handler is idempotent (compares
    ``exhausted_notified_at`` against ``exhausted_at``) so the daily
    poll re-dispatching for the same event is a no-op."""
    from apps.router.line_quota import is_monthly_limit_429, mark_quota_exhausted_from_429

    if not is_monthly_limit_429(status_code, body):
        return
    if mark_quota_exhausted_from_429():
        logger.warning("LINE Push monthly quota exhausted — fleet-wide gate engaged")
        try:
            from apps.cron.publish import publish_task

            publish_task("dispatch_line_quota_handler")
        except Exception:
            # Don't break the send path — the next daily poll will
            # re-dispatch via the same idempotent handler.
            logger.exception("line_quota: failed to enqueue handler dispatch from tripwire")


def _send_line_push(line_user_id: str, messages: list[dict]) -> bool:
    """Send messages via LINE Push Message API.

    Returns True on success.
    """
    return _post_line_push(line_user_id, messages) is not None


# ---------------------------------------------------------------------------
# Outbound message recording (for quote-reply context resolution)
# ---------------------------------------------------------------------------

_OUTBOUND_EXCERPT_MAX = 500
_OUTBOUND_PRUNE_DAYS = 30


def _message_text_excerpt(msg: dict) -> str:
    """Pull a human-readable excerpt from a LINE message dict so a future
    quote-reply lookup has something to quote.

    LINE messages come in many shapes (text, sticker, image, flex).
    Falls back across the fields that carry user-visible copy.
    """
    if not isinstance(msg, dict):
        return ""
    t = msg.get("type")
    if t == "text":
        return (msg.get("text") or "").strip()
    if t == "flex":
        return (msg.get("altText") or "").strip()
    if t in ("image", "video", "audio"):
        return f"[{t}]"
    if t == "sticker":
        return "[sticker]"
    return (msg.get("altText") or msg.get("text") or "").strip()


def _record_line_outbound(
    tenant,
    line_user_id: str,
    sent_messages: list[dict] | None,
    messages: list[dict],
) -> None:
    """Persist ``(id, text_excerpt)`` rows from a LINE send response so an
    inbound ``quotedMessageId`` can be resolved back to what we said.

    ``sent_messages`` is the ``sentMessages`` array LINE returns from the
    push/reply API; index-aligned with the ``messages`` we sent. We skip
    silently if either side is missing — recording is best-effort and
    must never break the send path.
    """
    if not sent_messages or not tenant or not line_user_id:
        return

    from apps.router.models import LineOutboundMessage

    rows: list[LineOutboundMessage] = []
    for i, sm in enumerate(sent_messages):
        if not isinstance(sm, dict):
            continue
        mid = sm.get("id")
        if not mid:
            continue
        excerpt = _message_text_excerpt(messages[i] if i < len(messages) else {})
        rows.append(
            LineOutboundMessage(
                tenant=tenant,
                line_user_id=line_user_id,
                line_message_id=str(mid),
                text_excerpt=excerpt[:_OUTBOUND_EXCERPT_MAX],
            )
        )
    if not rows:
        return

    try:
        LineOutboundMessage.objects.bulk_create(rows, ignore_conflicts=True)
    except Exception:
        logger.debug("LineOutboundMessage bulk_create failed", exc_info=True)
        return

    # Probabilistic pruning so the table can't grow unbounded — matches
    # the pattern used by ProcessedInboundEvent.
    import random as _random
    from datetime import timedelta

    if _random.random() < 0.01:
        cutoff = timezone.now() - timedelta(days=_OUTBOUND_PRUNE_DAYS)
        try:
            LineOutboundMessage.objects.filter(sent_at__lt=cutoff).delete()
        except Exception:
            logger.debug("LineOutboundMessage prune failed", exc_info=True)


def _extract_line_reply_context(tenant, message: dict) -> str:
    """If the inbound LINE ``message`` is a quote-reply, return a
    ``[Replying to: "..."]\\n\\n`` prefix; else empty string.

    Looks up ``message.quotedMessageId`` in ``LineOutboundMessage`` to
    find what we said in that earlier turn. Telegram gets this for free
    via ``reply_to_message.text`` in the webhook payload; LINE only
    sends the ID and requires our own store (see model docstring).
    """
    quoted_id = (message or {}).get("quotedMessageId")
    if not quoted_id or not tenant:
        return ""

    from apps.router.models import LineOutboundMessage

    try:
        row = (
            LineOutboundMessage.objects.filter(tenant=tenant, line_message_id=str(quoted_id))
            .only("text_excerpt")
            .first()
        )
    except Exception:
        logger.debug("LineOutboundMessage lookup failed", exc_info=True)
        return ""

    if not row or not row.text_excerpt:
        # We've seen the quoted id reference but lost the content — either
        # the row was pruned (>30d) or the assistant message was sent
        # before this feature shipped. Still annotate so the agent knows
        # the user is replying to something it said.
        return "[Replying to an earlier message of yours]\n\n"

    excerpt = row.text_excerpt
    if len(excerpt) > 200:
        excerpt = excerpt[:200] + "…"
    return f'[Replying to: "{excerpt}"]\n\n'


def _send_line_text(line_user_id: str, text: str) -> bool:
    """Send a single text message via LINE Push API."""
    # LINE max message length is 5000 chars
    if len(text) > 5000:
        # Split into chunks
        chunks = _split_message(text, max_len=5000)
        # LINE allows max 5 messages per push
        for i in range(0, len(chunks), 5):
            batch = [{"type": "text", "text": c} for c in chunks[i : i + 5]]
            if not _send_line_push(line_user_id, batch):
                return False
        return True
    return _send_line_push(line_user_id, [{"type": "text", "text": text}])


def _send_line_flex(line_user_id: str, flex_msg: dict) -> bool:
    """Send a single Flex message via LINE Push API."""
    return _send_line_push(line_user_id, [flex_msg])


def _split_message(text: str, max_len: int = 5000) -> list[str]:
    """Split a long message into chunks."""
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


def _strip_markdown(text: str) -> str:
    """Strip common markdown formatting for LINE (which doesn't support it).

    Converts bold/italic markers to plain text, keeps links as URLs.
    Converts markdown tables to readable plain text.
    """
    # Convert markdown tables to readable format
    text = _convert_tables(text)
    # Remove code blocks (``` ... ```)
    text = re.sub(r"```[^\n]*\n(.*?)```", r"\1", text, flags=re.DOTALL)
    # Remove bold markers
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    # Remove italic markers
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    # Convert markdown links to plain URLs
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1: \2", text)
    # Remove inline code markers
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Remove heading markers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return text


def _convert_tables(text: str) -> str:
    """Convert markdown tables to readable plain text for LINE.

    | Exercise | Sets | Rest |      →   Exercise: Pull-Ups
    |----------|------|------|           Sets: 4 × 6-10
    | Pull-Ups | 4×6-10 | 90s |        Rest: 90s
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Detect table: line starts and ends with | and has multiple |
        if line.startswith("|") and line.endswith("|") and line.count("|") >= 3:
            # Collect all table lines
            table_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1

            # Parse header + data rows
            parsed = _parse_table(table_lines)
            if parsed:
                result.append(parsed)
            else:
                result.extend(table_lines)
        else:
            result.append(lines[i])
            i += 1

    return "\n".join(result)


def _parse_table(table_lines: list[str]) -> str | None:
    """Parse markdown table lines into readable text."""
    if len(table_lines) < 2:
        return None

    def split_row(line: str) -> list[str]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        return [c for c in cells if c]

    headers = split_row(table_lines[0])
    if not headers:
        return None

    # Skip separator rows (|---|---|)
    data_rows = []
    for row_line in table_lines[1:]:
        cells = split_row(row_line)
        # Skip separator rows
        if cells and all(re.match(r"^[-:]+$", c) for c in cells):
            continue
        if cells:
            data_rows.append(cells)

    if not data_rows:
        return None

    # Format: each data row as "header: value" pairs
    output_parts: list[str] = []
    for row in data_rows:
        pairs = []
        for j, cell in enumerate(row):
            if j < len(headers):
                pairs.append(f"{headers[j]}: {cell}")
            else:
                pairs.append(cell)
        output_parts.append("\n".join(pairs))

    return "\n\n".join(output_parts)


def _resolve_tenant_by_line_user_id(line_user_id: str) -> Tenant | None:
    """Resolve tenant by LINE user ID."""
    try:
        user = User.objects.select_related("tenant").get(line_user_id=line_user_id)
        tenant = user.tenant

        if tenant.status in (
            Tenant.Status.ACTIVE,
            Tenant.Status.SUSPENDED,
            Tenant.Status.PENDING,
            Tenant.Status.PROVISIONING,
        ):
            return tenant
        return None
    except (User.DoesNotExist, Tenant.DoesNotExist):
        return None


def _send_line_follow_up(tenant: Tenant, text: str) -> None:
    """Send a simple text follow-up via LINE Push API (for callback confirmations)."""
    line_user_id = getattr(tenant.user, "line_user_id", None)
    if not line_user_id:
        return
    _send_line_push(line_user_id, [{"type": "text", "text": text}])


def _send_line_status_bubble(tenant: Tenant, text: str, tone: str = "success") -> None:
    """Send a branded status bubble via LINE Push API."""
    from apps.router.line_flex import build_status_bubble

    line_user_id = getattr(tenant.user, "line_user_id", None)
    if not line_user_id:
        return
    _send_line_push(line_user_id, [build_status_bubble(text, tone=tone)])


def relay_ai_response_to_line(
    tenant: Tenant,
    line_user_id: str,
    ai_text: str,
    reply_token: str | None = None,
) -> bool:
    """Format and deliver an AI assistant response to LINE.

    Single source of truth for outbound AI replies — used by both the live
    webhook and the hibernation buffered-delivery path so they share PII
    rehydration, [[chart:]] rendering, MEDIA stripping, quick-reply
    extraction, table conversion, code-block stripping, Flex bubble
    construction, and emergency plain-text fallback.

    Sends via Reply API (free) when ``reply_token`` is supplied, otherwise
    Push API. Returns True on successful delivery.
    """
    if not ai_text or not line_user_id:
        return False

    # Rehydrate PII placeholders before sending to user
    entity_map = getattr(tenant, "pii_entity_map", None)
    if entity_map:
        from apps.pii.redactor import rehydrate_text

        ai_text = rehydrate_text(ai_text, entity_map)

    # Log-only instrumentation: did the agent draw an ASCII chart instead of
    # emitting a [[chart:...]] marker? See apps/router/output_guards.py.
    from apps.router.output_guards import log_ascii_chart_leak

    log_ascii_chart_leak(ai_text, tenant_id=tenant.id, channel="line")

    # Extract [[insight:slug]]statement[[/insight]] markers and write
    # AssistantInsight rows before chart processing (same flow as
    # pending_queue.py / poller.py). The reply text is updated in place
    # so chart markers nested inside or near insight markers still match.
    try:
        from apps.insights.markers import extract_and_record_insights

        ai_text = extract_and_record_insights(ai_text, tenant=tenant)
    except Exception:
        logger.exception("insight marker extraction failed (line webhook)")

    try:
        # Render [[chart:type]] markers into images
        image_messages: list[dict] = []
        chart_pattern = re.compile(r"\[\[chart:(\w+)(?:\|(.+?))?\]\]")
        for match in chart_pattern.finditer(ai_text):
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
                    chart_url = f"{settings.API_BASE_URL}/api/v1/charts/{tenant.id}/{fname.split('/')[-1]}"
                    image_messages.append(
                        {
                            "type": "image",
                            "originalContentUrl": chart_url,
                            "previewImageUrl": chart_url,
                        }
                    )
            except Exception:
                logger.exception("Chart rendering failed for %s (LINE)", chart_type)
        ai_text = chart_pattern.sub("", ai_text)

        # Strip MEDIA markers (image sending from workspace not yet supported on LINE)
        clean_text = re.sub(r"MEDIA:\S+", "", ai_text).strip()

        # Extract quick reply buttons before stripping markdown
        clean_text, quick_reply_items = extract_quick_reply_buttons(clean_text)

        # Pre-process: convert tables and strip code blocks BEFORE Flex decision
        # (both Flex and plain text paths need these gone)
        clean_text = _convert_tables(clean_text)
        clean_text = re.sub(r"```[^\n]*\n(.*?)```", r"\1", clean_text, flags=re.DOTALL)

        # Build Flex message (branded bubbles for all content types)
        messages: list[dict] = []
        try:
            flex_msg = build_flex_bubble(clean_text, alt_text=_strip_markdown(clean_text))
            messages = [flex_msg]
        except Exception:
            # Flex construction failed — fall back to plain text
            logger.debug("Flex build failed, falling back to plain text", exc_info=True)
            plain = _strip_markdown(clean_text)
            plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
            if plain:
                chunks = _split_message(plain, max_len=5000)
                messages = [{"type": "text", "text": c} for c in chunks[:5]]

        # Prepend chart images before text/Flex messages
        if image_messages:
            messages = image_messages + messages

        # Attach quick replies to the last message
        if messages and quick_reply_items:
            messages[-1] = attach_quick_reply(messages[-1], quick_reply_items)

        # LINE Push API allows max 5 messages per request
        messages = messages[:5]

        if not messages:
            return False

        sent = _send_line_messages(line_user_id, messages, reply_token=reply_token, tenant=tenant)
        if sent:
            return True

        logger.error(
            "LINE response delivery failed for %s (container=%s)",
            line_user_id,
            tenant.container_fqdn,
        )
        # Retry with plain text as emergency fallback
        fallback_text = _strip_markdown(ai_text)
        return _send_line_messages(
            line_user_id,
            [{"type": "text", "text": fallback_text[:5000]}],
            tenant=tenant,
        )

    except Exception:
        logger.exception("Error building LINE response for %s", line_user_id)
        # Emergency fallback — strip markdown before sending
        fallback_text = _strip_markdown(ai_text)
        return _send_line_messages(
            line_user_id,
            [{"type": "text", "text": fallback_text[:5000]}],
            tenant=tenant,
        )


@method_decorator(csrf_exempt, name="dispatch")
class LineWebhookView(View):
    """Receive and process LINE webhook events."""

    def post(self, request) -> HttpResponse:
        # 1. Verify signature
        signature = request.headers.get("X-Line-Signature", "")
        if not signature:
            return HttpResponse(status=403)

        if not _verify_signature(request.body, signature):
            logger.warning("LINE webhook: invalid signature")
            return HttpResponse(status=403)

        # 2. Parse events
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return HttpResponse(status=400)

        events = body.get("events", [])

        # 3. Process each event asynchronously
        # LINE requires 200 within 1 second — never block here.
        # In tests we run handlers inline so daemon threads don't hold DB
        # connections past test_db teardown.
        disable_threads = getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False)
        for event in events:
            if disable_threads:
                self._handle_event(event)
            else:
                threading.Thread(
                    target=self._handle_event,
                    args=(event,),
                    daemon=True,
                ).start()

        return HttpResponse(status=200)

    def _handle_event(self, event: dict) -> None:
        """Route a single LINE event to the appropriate handler."""
        # Set service-role RLS context for DB access (safe no-op on SQLite)
        try:
            from apps.tenants.middleware import set_rls_context

            set_rls_context(service_role=True)
        except Exception:
            pass

        # Idempotency gate — LINE delivers at least once and redelivers
        # (same webhookEventId, deliveryContext.isRedelivery=true) when we
        # ack slowly. Claim the event before any side effect so a
        # redelivery can't spawn a second PendingMessage / reply.
        from apps.router.inbound_dedup import claim_inbound_event

        webhook_event_id = event.get("webhookEventId")
        if not claim_inbound_event(f"line:{webhook_event_id}" if webhook_event_id else None):
            logger.info(
                "LINE webhook: skipping duplicate event %s (isRedelivery=%s)",
                webhook_event_id,
                event.get("deliveryContext", {}).get("isRedelivery"),
            )
            return

        try:
            event_type = event.get("type", "")
            if event_type == "message":
                self._handle_message(event)
            elif event_type == "follow":
                self._handle_follow(event)
            elif event_type == "unfollow":
                self._handle_unfollow(event)
            elif event_type == "postback":
                self._handle_postback(event)
            else:
                logger.debug("LINE webhook: unhandled event type %s", event_type)
        except Exception:
            logger.exception("Error handling LINE event: %s", event.get("type"))

    def _handle_follow(self, event: dict) -> None:
        """User added the bot — send welcome message."""
        line_user_id = event.get("source", {}).get("userId", "")
        if not line_user_id:
            return

        frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")

        # Check if already linked
        tenant = _resolve_tenant_by_line_user_id(line_user_id)
        if tenant:
            _send_line_flex(
                line_user_id,
                build_short_bubble(
                    f"Welcome back, {tenant.user.display_name}! \U0001f44b\n\n"
                    "Your LINE account is already connected. You can start chatting!",
                ),
            )
            return

        _send_line_flex(
            line_user_id,
            build_flex_bubble(
                "## Welcome to Neighborhood United!\n\n"
                "To connect your account:\n"
                f"1. Sign up at {frontend_url}\n"
                "2. Go to Settings \u2192 Connect LINE\n"
                "3. Tap the link or scan the QR code\n\n"
                "Once connected, you can chat with your AI assistant right here!",
            ),
        )

    def _handle_unfollow(self, event: dict) -> None:
        """User blocked/unfollowed the bot — clear line_user_id."""
        line_user_id = event.get("source", {}).get("userId", "")
        if not line_user_id:
            return

        try:
            user = User.objects.get(line_user_id=line_user_id)
            user.line_user_id = None
            user.line_display_name = ""
            if user.preferred_channel == "line":
                user.preferred_channel = "telegram"
            user.save(update_fields=["line_user_id", "line_display_name", "preferred_channel"])
            logger.info("LINE unfollow: cleared line_user_id for user %s", user.id)
        except User.DoesNotExist:
            pass

    def _handle_message(self, event: dict) -> None:
        """Handle incoming text or audio message."""
        message = event.get("message", {})
        msg_type = message.get("type", "")
        reply_token = event.get("replyToken")
        line_user_id = event.get("source", {}).get("userId", "")
        # Set when the audio branch resolves the tenant early (to gate voice
        # during onboarding before the paid Whisper call); the shared text flow
        # below reuses it instead of resolving the same LINE user twice. The
        # flag distinguishes "resolved to None (unrecognized)" from "not yet
        # resolved" so we never resolve a second time.
        prefetched_tenant: Tenant | None = None
        tenant_prefetched = False

        # Audio/voice messages — transcribe via Whisper
        if msg_type == "audio":
            if not line_user_id:
                return
            message_id = message.get("id")
            if not message_id:
                return
            logger.info(
                "LINE audio received: message_id=%s from %s",
                message_id,
                line_user_id,
            )
            # Reject voice during onboarding BEFORE incurring the paid Whisper
            # call: a user still onboarding (or eligible for re-introduction) can
            # only answer with typed text, so transcribing here would waste an API
            # call on a discarded transcript and leave the user staring at a long
            # loading animation before the rejection. This mirrors the downstream
            # needs_reintroduction gate so that backfilled/default-profile users are
            # also caught here rather than after paying for a Whisper call.
            _audio_tenant = _resolve_tenant_by_line_user_id(line_user_id)
            from apps.router.onboarding import needs_reintroduction as _needs_reintroduction

            if _audio_tenant is not None and (
                not _audio_tenant.onboarding_complete
                or _audio_tenant.onboarding_step == 0
                or _needs_reintroduction(_audio_tenant)
            ):
                _send_line_flex(
                    line_user_id,
                    build_short_bubble("I'll be ready for stickers and voice soon! For now, please type your answer."),
                )
                return
            # Reuse this resolution downstream so the shared text flow doesn't
            # resolve the same LINE user a second time after transcription.
            prefetched_tenant = _audio_tenant
            tenant_prefetched = True
            _show_loading(line_user_id)
            transcript = _transcribe_line_audio(message_id)
            if transcript:
                logger.info(
                    "LINE audio transcribed: %d chars from message_id=%s",
                    len(transcript),
                    message_id,
                )
                # Re-show loading — Whisper may have consumed most of the first one
                _show_loading(line_user_id)
                # Process the transcribed text as if it were a text message
                text = f'🎤 Voice message: "{transcript}"'
                # Fall through to normal text processing below
            else:
                _send_line_flex(
                    line_user_id,
                    build_status_bubble(
                        "Sorry, I couldn't transcribe that audio. Please try again or send a text message.",
                        tone="warning",
                    ),
                )
                return
        elif msg_type == "sticker":
            # LINE stickers carry emotion/intent via keywords
            keywords = message.get("keywords", [])
            sticker_resource = message.get("stickerResourceType", "")
            package_id = message.get("packageId", "")
            sticker_id = message.get("stickerId", "")
            if keywords:
                keyword_str = ", ".join(keywords[:5])
                text = (
                    f"[User sent a LINE sticker expressing: {keyword_str}. "
                    f"Respond naturally to the emotion — keep it brief, "
                    f"use emoji to match the vibe.]"
                )
            else:
                text = (
                    "[User sent a LINE sticker (no keywords available). "
                    "Respond warmly with a matching emoji — treat it like "
                    "a friendly reaction.]"
                )
            logger.info(
                "LINE sticker: pkg=%s id=%s type=%s keywords=%s",
                package_id,
                sticker_id,
                sticker_resource,
                keywords,
            )
        elif msg_type == "text":
            text = message.get("text", "").strip()
        else:
            # Unsupported message types (image, video, location, etc.)
            if line_user_id:
                _send_line_flex(
                    line_user_id,
                    build_status_bubble(
                        "I can process text, voice, and stickers. Please send one of those!",
                        tone="warning",
                    ),
                )
            return

        if not text or not line_user_id:
            return

        # Check for link token (format: link_TOKEN)
        if text.startswith("link_"):
            token = text[5:]  # Strip "link_" prefix
            self._process_link(line_user_id, event, token)
            return

        # Resolve tenant (reuse the audio branch's early resolution if present)
        tenant = prefetched_tenant if tenant_prefetched else _resolve_tenant_by_line_user_id(line_user_id)

        if not tenant:
            frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
            _send_line_flex(
                line_user_id,
                build_status_bubble(
                    "I don't recognize your account yet.\n\n"
                    f"Sign up at {frontend_url} and connect LINE "
                    "from your Settings page to get started!",
                    tone="warning",
                ),
            )
            return

        # Provisioning tenant
        if tenant.status in (Tenant.Status.PENDING, Tenant.Status.PROVISIONING):
            lang = tenant.user.language or "en"
            _send_line_flex(
                line_user_id,
                build_short_bubble(error_msg(lang, "waking_up")),
            )
            return

        # Paused tenant — trial ended or payment lapsed
        frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
        if tenant.status == Tenant.Status.SUSPENDED and not tenant.is_trial and not bool(tenant.stripe_subscription_id):
            lang = tenant.user.language or "en"
            _send_line_flex(
                line_user_id,
                build_status_bubble(
                    error_msg(
                        lang,
                        "suspended",
                        billing_url=f"{frontend_url}/settings/billing",
                    ),
                    tone="warning",
                ),
            )
            return

        # Budget check — before wake so we don't start a container just to block it
        budget_reason = check_budget(tenant)
        if budget_reason:
            from apps.router.views import _hibernate_for_quota

            _hibernate_for_quota(tenant)
            lang = tenant.user.language or "en"
            if budget_reason == "global":
                msg_key = "budget_unavailable"
                kwargs: dict[str, str] = {}
            else:
                msg_key = "budget_exhausted_trial" if tenant.is_trial else "budget_exhausted_paid"
                kwargs = {
                    "plus_message": "",
                    "billing_url": f"{frontend_url}/billing",
                }
            _send_line_flex(
                line_user_id,
                build_status_bubble(error_msg(lang, msg_key, **kwargs), tone="warning"),
            )
            return

        # Hibernated tenant — buffer message and wake container
        from apps.router.wake_on_message import (
            ACK_FRESH,
            ACK_RECONNECT,
            SILENT,
            handle_hibernated_message,
        )

        wake_result = handle_hibernated_message(tenant, "line", event, text)
        if wake_result == ACK_FRESH:
            lang = tenant.user.language or "en"
            _send_line_flex(
                line_user_id,
                build_short_bubble(error_msg(lang, "hibernation_waking")),
            )
            return
        elif wake_result == ACK_RECONNECT:
            lang = tenant.user.language or "en"
            _send_line_flex(
                line_user_id,
                build_short_bubble(error_msg(lang, "hibernation_reconnecting")),
            )
            return
        elif wake_result == SILENT:
            return

        # Onboarding / re-introduction gate
        from apps.router.onboarding import get_onboarding_response, needs_reintroduction

        if needs_reintroduction(tenant):
            tenant.onboarding_step = 0
            tenant.save(update_fields=["onboarding_step", "updated_at"])

        if not tenant.onboarding_complete or tenant.onboarding_step == 0:
            # During onboarding, only accept typed text
            if msg_type != "text":
                _send_line_flex(
                    line_user_id,
                    build_short_bubble("I'll be ready for stickers and voice soon! For now, please type your answer."),
                )
                return
            # LINE has no language_code — always ask language question
            onboarding_reply = get_onboarding_response(tenant, text, telegram_lang="")
            if onboarding_reply is not None:
                self._send_onboarding_reply(line_user_id, onboarding_reply)
                return

        # Show loading animation while LLM processes
        _show_loading(line_user_id)

        # Update last_message_at
        Tenant.objects.filter(id=tenant.id).update(last_message_at=timezone.now())

        # If this is a quote-reply, prepend reply-context prefix so the
        # agent knows what message it's responding to. Mirror of Telegram's
        # ``_extract_reply_context`` in poller.py.
        reply_prefix = _extract_line_reply_context(tenant, message)
        forwarded_text = f"{reply_prefix}{text}" if reply_prefix else text
        raw_user_text = text  # apology fallback should not include the prefix

        # Forward to container (pass reply_token for free Reply API)
        self._forward_to_container(
            line_user_id,
            tenant,
            forwarded_text,
            reply_token=reply_token,
            is_voice=msg_type == "audio",
            raw_user_text=raw_user_text,
        )

    def _send_onboarding_reply(self, line_user_id: str, reply) -> None:
        """Render an OnboardingReply as a LINE Flex message with optional Quick Reply buttons."""
        msg = build_short_bubble(reply.text)
        if reply.keyboard:
            items = telegram_keyboard_to_quick_reply(reply.keyboard)
            msg = attach_quick_reply(msg, items)
        _send_line_push(line_user_id, [msg])

    def _handle_onboarding_postback(self, tenant: Tenant, line_user_id: str, data: str) -> None:
        """Handle onboarding button callbacks (tz_country:*, tz_zone:*)."""
        from apps.router.onboarding import handle_onboarding_callback

        reply = handle_onboarding_callback(tenant, data)
        if reply is not None:
            self._send_onboarding_reply(line_user_id, reply)

    def _process_link(self, line_user_id: str, event: dict, token: str) -> None:
        """Process account linking via link token."""
        from apps.router.line_service import process_line_link_token

        # Get display name from LINE profile (best effort)
        display_name = self._get_line_profile_name(line_user_id) or ""

        success, reply = process_line_link_token(
            line_user_id=line_user_id,
            line_display_name=display_name,
            token=token,
        )

        _send_line_text(line_user_id, reply)

    def _get_line_profile_name(self, line_user_id: str) -> str | None:
        """Fetch user's display name from LINE Profile API."""
        access_token = _get_access_token()
        if not access_token:
            return None

        try:
            resp = httpx.get(
                f"{LINE_API_BASE}/profile/{line_user_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=5,
            )
            if resp.is_success:
                return resp.json().get("displayName")
        except Exception:
            logger.debug("Failed to fetch LINE profile for %s", line_user_id)
        return None

    def _forward_to_container(
        self,
        line_user_id: str,
        tenant: Tenant,
        message_text: str,
        reply_token: str | None = None,
        is_voice: bool = False,
        raw_user_text: str | None = None,
    ) -> None:
        """Pre-process the message and enqueue it on the per-tenant
        serialization queue.

        We DON'T POST to the container here anymore — that happens in
        ``apps.router.pending_queue.drain_pending_messages_for_tenant_task``
        (PR #431). Doing the POST inline meant a second message arriving
        while the first was still in flight would land an overlapping
        turn at the OpenClaw claude-cli backend, which rejects concurrent
        turns and (pre-#427) silently fell back to MiniMax. Serializing
        per ``(tenant, channel, line_user_id)`` keeps the live claude
        session intact and preserves conversation context across
        rapid-fire messages.

        Pre-flight state checks (provisioning, container_fqdn) stay
        synchronous so we can give the user immediate feedback instead
        of enqueuing a row that's guaranteed to fail.
        """
        from apps.router.pending_queue import enqueue_message_for_tenant

        # Preserve the user-meaningful text for the dropped-message apology.
        # ``message_text`` accumulates workspace/transition/datetime/chat
        # markers below — agent-only metadata that would render the apology
        # excerpt useless ("It started with: '[Now: …'").
        raw_user_text = raw_user_text if raw_user_text is not None else message_text

        # PII redaction for outgoing LLM-provider traffic. Redact the BARE
        # user text here, BEFORE the proactive/datetime/chat markers are
        # prepended below — running redaction over the assembled body makes
        # the NER detector misfire on the structural markers. Mirrors the
        # Telegram poller seam (poller.py:_forward_to_container). Outbound
        # rehydration is already wired (line 657), so [PERSON_N] placeholders
        # round-trip. ``redact_user_message`` swallows its own errors and
        # returns the original text, so redaction never blocks delivery.
        from apps.pii.redactor import redact_user_message

        message_text = redact_user_message(message_text, tenant)
        raw_user_text = redact_user_message(raw_user_text, tenant)

        lang = tenant.user.language or "en"

        if not tenant.container_fqdn or tenant.status == "provisioning":
            _send_line_flex(
                line_user_id,
                build_short_bubble(error_msg(lang, "provisioning_setup")),
            )
            return

        user_tz = tenant.user.timezone or "UTC"

        # Chat sessionKey is flat: one continuous session per user.
        # Workspace-based chat routing was removed 2026-05-20 — see
        # docs/implementation/remove-workspace-chat-routing.md. Cron
        # isolation lives at sessionTarget="isolated" + isolatedSession=True
        # on the cron job config, not in user_param.
        user_param = line_user_id

        # Inject current time so the agent always knows "now"
        # Surface any proactive outbound (cron-fired or otherwise) sent
        # to this user in the last 24h so the agent can thread the
        # reply back to it. See apps.router.proactive_context.
        from apps.router.proactive_context import surface_proactive_context
        from apps.router.services import (
            build_chat_context_marker,
            build_datetime_context,
        )

        proactive_block = surface_proactive_context(
            tenant=tenant,
            channel="line",
            channel_user_id=line_user_id,
        )

        # Mark this as a conversational turn (not a scheduled cron run) so the
        # agent skips the heavy AGENTS.md "Session Start" auto-context-load.
        # See poller.py for the parallel comment.
        message_text = proactive_block + build_datetime_context(user_tz) + build_chat_context_marker() + message_text

        # Hand off to the serialization queue. The reply_token is
        # captured but the drain task ignores it — by the time the
        # queue runs (potentially many seconds after the webhook),
        # LINE's reply window has typically closed and we have to
        # Push anyway. Captured here for forensic logging only.
        enqueue_message_for_tenant(
            tenant=tenant,
            channel="line",
            channel_user_id=line_user_id,
            payload={
                "message_text": message_text,
                "user_param": user_param,
                "user_timezone": user_tz,
                "is_voice": bool(is_voice),
                "reply_token": reply_token,
            },
            user_text_excerpt=raw_user_text,
        )

    def _handle_postback(self, event: dict) -> None:
        """Handle postback events (inline button callbacks).

        Phase 1: Basic support for extraction callbacks.
        """
        line_user_id = event.get("source", {}).get("userId", "")
        data = event.get("postback", {}).get("data", "")

        if not line_user_id or not data:
            return

        tenant = _resolve_tenant_by_line_user_id(line_user_id)
        if not tenant:
            return

        # Onboarding callbacks (country/timezone buttons)
        if data.startswith("tz_"):
            self._handle_onboarding_postback(tenant, line_user_id, data)
            return

        # Action gate callbacks
        if data.startswith("gate_approve:") or data.startswith("gate_deny:"):
            self._handle_gate_postback(tenant, data)
            return

        # Extraction approval callbacks (nightly extraction)
        if data.startswith("extract:"):
            self._handle_extraction_postback(tenant, data)
            return

        # Reconciliation task-action undo callbacks (nightly extraction deltas)
        if data.startswith("task_action:"):
            self._handle_task_action_postback(tenant, data)
            return

        # Lesson approval callbacks (agent-suggested)
        if data.startswith("lesson:"):
            self._handle_lesson_postback(tenant, data)
            return

        # Generic button forwards spawn a real AI turn, so they must clear the
        # same suspension + budget pre-flight gate as a typed message — otherwise
        # a tapped button skips the gate a typed message enforces and can land a
        # billable turn on a suspended/over-budget tenant. (The special-prefix
        # branches above act on stored DB state and don't spawn turns.)
        frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
        if (
            tenant.status == Tenant.Status.SUSPENDED
            and not tenant.is_trial
            and not bool(tenant.stripe_subscription_id)
        ):
            lang = tenant.user.language or "en"
            _send_line_flex(
                line_user_id,
                build_status_bubble(
                    error_msg(lang, "suspended", billing_url=f"{frontend_url}/settings/billing"),
                    tone="warning",
                ),
            )
            return

        budget_reason = check_budget(tenant)
        if budget_reason:
            from apps.router.views import _hibernate_for_quota

            _hibernate_for_quota(tenant)
            lang = tenant.user.language or "en"
            if budget_reason == "global":
                msg_key = "budget_unavailable"
                kwargs: dict[str, str] = {}
            else:
                msg_key = "budget_exhausted_trial" if tenant.is_trial else "budget_exhausted_paid"
                kwargs = {"plus_message": "", "billing_url": f"{frontend_url}/billing"}
            _send_line_flex(
                line_user_id,
                build_status_bubble(error_msg(lang, msg_key, **kwargs), tone="warning"),
            )
            return

        # Forward postback data as a message to the agent
        self._forward_to_container(
            line_user_id,
            tenant,
            f'[User tapped button: "{data}"]',
        )

    @staticmethod
    def _handle_gate_postback(tenant, data: str) -> None:
        """Handle action gate approve/deny from LINE postback."""
        from django.utils import timezone

        from apps.actions.messaging import update_gate_message
        from apps.actions.models import ActionAuditLog, ActionStatus, PendingAction

        try:
            parts = data.split(":")
            if len(parts) != 2:
                return

            is_approve = parts[0] == "gate_approve"
            action_id = int(parts[1])

            action = PendingAction.objects.get(id=action_id, tenant=tenant)

            if action.status != ActionStatus.PENDING:
                return

            # Expired? Use conditional UPDATE so the sweep cannot have already
            # flipped the row between our is_expired check and the write.
            if action.is_expired:
                updated = PendingAction.objects.filter(
                    id=action.id,
                    status=ActionStatus.PENDING,
                ).update(status=ActionStatus.EXPIRED)
                if updated:
                    action.status = ActionStatus.EXPIRED
                    ActionAuditLog.objects.create(
                        tenant=tenant,
                        action_type=action.action_type,
                        action_payload=action.action_payload,
                        display_summary=action.display_summary,
                        result=ActionStatus.EXPIRED,
                    )
                    update_gate_message(action)
                return

            # Apply response using a conditional UPDATE so the sweep cannot
            # clobber an approve that lands at the deadline boundary.
            now = timezone.now()
            new_status = ActionStatus.APPROVED if is_approve else ActionStatus.DENIED
            updated = PendingAction.objects.filter(
                id=action.id,
                status=ActionStatus.PENDING,
            ).update(status=new_status, responded_at=now)
            if not updated:
                # Sweep flipped EXPIRED between our read and write; nothing to do.
                return
            action.status = new_status
            action.responded_at = now

            ActionAuditLog.objects.create(
                tenant=tenant,
                action_type=action.action_type,
                action_payload=action.action_payload,
                display_summary=action.display_summary,
                result=action.status,
                responded_at=now,
            )

            update_gate_message(action)

        except PendingAction.DoesNotExist:
            logger.warning("Gate postback: action %s not found", data)
        except Exception:
            logger.exception("Error handling gate postback: %s", data)

    @staticmethod
    def _handle_extraction_postback(tenant, data: str) -> None:
        """Handle extraction approval/dismissal/undo from LINE postback.

        Data format: extract:<action>:<pending_id>
        """
        from django.utils import timezone as tz

        from apps.journal.models import PendingExtraction
        from apps.router.extraction_callbacks import (
            _approve_goal,
            _approve_lesson,
            _approve_task,
            _undo_goal,
            _undo_lesson,
            _undo_task,
        )

        try:
            parts = data.split(":")
            if len(parts) != 3:
                return

            _, action, pending_id = parts
            is_undo = action in ("undo_lesson", "undo_goal", "undo_task")

            pending = PendingExtraction.objects.filter(
                id=pending_id,
                tenant=tenant,
            ).first()
            if not pending:
                return

            # Handle undo actions (operate on APPROVED items)
            if is_undo:
                if pending.status == PendingExtraction.Status.UNDONE:
                    _send_line_follow_up(tenant, "👍 Already removed!")
                    return
                if pending.status != PendingExtraction.Status.APPROVED:
                    _send_line_follow_up(tenant, "👍 Can't undo — not currently added.")
                    return

                if action == "undo_lesson":
                    _undo_lesson(pending)
                elif action == "undo_goal":
                    _undo_goal(pending)
                elif action == "undo_task":
                    _undo_task(pending)

                pending.status = PendingExtraction.Status.UNDONE
                pending.resolved_at = tz.now()
                pending.save(update_fields=["status", "resolved_at"])
                _send_line_status_bubble(tenant, f"Removed: {pending.text[:80]}", tone="warning")
                return

            # Already resolved — send friendly acknowledgment instead of silence
            if pending.status != PendingExtraction.Status.PENDING:
                status_text = (
                    "already added" if pending.status == PendingExtraction.Status.APPROVED else "already skipped"
                )
                _send_line_follow_up(tenant, f"👍 That one's {status_text}!")
                return

            if action == "dismiss":
                pending.status = PendingExtraction.Status.DISMISSED
                pending.resolved_at = tz.now()
                pending.save(update_fields=["status", "resolved_at"])
                _send_line_status_bubble(tenant, f"Skipped: {pending.text[:80]}", tone="warning")
                return

            if action == "approve_lesson":
                _, lesson_id = _approve_lesson(pending)
            elif action == "approve_goal":
                _approve_goal(pending)
            elif action == "approve_task":
                _approve_task(pending)
            else:
                return

            pending.status = PendingExtraction.Status.APPROVED
            pending.resolved_at = tz.now()
            if action == "approve_lesson" and lesson_id:
                pending.lesson_id = lesson_id
                pending.save(update_fields=["status", "resolved_at", "lesson_id"])
            else:
                pending.save(update_fields=["status", "resolved_at"])

            kind_label = {"lesson": "Saved to constellation", "goal": "Added to goals", "task": "Added to tasks"}.get(
                pending.kind, "Added"
            )
            _send_line_status_bubble(tenant, f"{kind_label}: {pending.text[:80]}", tone="success")

        except Exception:
            logger.exception("Error handling extraction postback: %s", data)

    @staticmethod
    def _handle_task_action_postback(tenant, data: str) -> None:
        """Handle reconciliation PendingTaskAction undo from LINE postback.

        Data format: task_action:undo:<action_id>
        """
        from apps.router.task_action_callbacks import handle_task_action_postback_line

        try:
            ok, message = handle_task_action_postback_line(tenant, data)
            tone = "success" if ok else "warning"
            _send_line_status_bubble(tenant, message, tone=tone)
        except Exception:
            logger.exception("Error handling task_action postback: %s", data)

    @staticmethod
    def _handle_lesson_postback(tenant, data: str) -> None:
        """Handle lesson approval/dismissal from LINE postback.

        Data format: lesson:<action>:<lesson_id>
        """
        from django.utils import timezone as tz

        from apps.lessons.models import Lesson
        from apps.lessons.services import process_approved_lesson

        try:
            parts = data.split(":")
            if len(parts) != 3:
                return

            action = parts[1]
            lesson_id = int(parts[2])

            lesson = Lesson.objects.filter(id=lesson_id, tenant=tenant, status="pending").first()
            if not lesson:
                # Already resolved — acknowledge instead of leaving a stale card's
                # button feeling dead. Stay silent only if the lesson is truly gone.
                resolved = Lesson.objects.filter(id=lesson_id, tenant=tenant).first()
                if resolved is not None:
                    status_text = "already saved" if resolved.status == "approved" else "already skipped"
                    _send_line_follow_up(tenant, f"👍 That one's {status_text}!")
                return

            from apps.pii.redactor import rehydrate_for_tenant

            safe_text = rehydrate_for_tenant(tenant, lesson.text)

            if action == "approve":
                lesson.status = "approved"
                lesson.approved_at = tz.now()
                lesson.save(update_fields=["status", "approved_at"])

                try:
                    process_approved_lesson(lesson)
                except Exception:
                    logger.exception("Failed to process approved lesson %s", lesson_id)

                _send_line_follow_up(tenant, f"✅ Approved: {safe_text[:100]}")

            elif action == "dismiss":
                lesson.status = "dismissed"
                lesson.save(update_fields=["status"])
                _send_line_follow_up(tenant, f"❌ Dismissed: {safe_text[:100]}")

        except Exception:
            logger.exception("Error handling lesson postback: %s", data)

    # Gateway error strings that should be treated as empty responses
    _GATEWAY_ERROR_STRINGS = frozenset(
        {
            "No response from OpenClaw.",
            "No response from OpenClaw",
        }
    )

    @staticmethod
    def _extract_ai_response(result: dict) -> str | None:
        """Extract AI response text from chat completions response.

        Returns None if the response is empty or contains a gateway
        error string (e.g. 'No response from OpenClaw.').
        """
        try:
            choices = result.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content")
                if text and text.strip() not in LineWebhookView._GATEWAY_ERROR_STRINGS:
                    return text
        except (IndexError, KeyError, TypeError):
            pass
        return None

    @staticmethod
    def _record_usage(tenant: Tenant, result: dict) -> None:
        """Record token usage from the response."""
        usage = result.get("usage", {})
        if not isinstance(usage, dict):
            return

        input_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
        output_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
        model_used = resolve_model_for_attribution(tenant, result)

        if input_tokens or output_tokens:
            try:
                record_usage(
                    tenant=tenant,
                    event_type="message",
                    input_tokens=int(input_tokens),
                    output_tokens=int(output_tokens),
                    model_used=model_used,
                )
            except Exception:
                logger.exception("Failed to record usage for tenant %s", tenant.id)
