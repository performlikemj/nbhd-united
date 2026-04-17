"""Endpoint for tenant agents to send messages to users via Django poller.

Used by cron jobs and proactive agent actions. Routes messages through
the central Telegram bot or LINE Push API, depending on user preference.
"""

from __future__ import annotations

import logging

import httpx
from django.conf import settings
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Rate limit: max 20 messages per hour per tenant (prevents runaway cron loops)
RATE_LIMIT_PER_HOUR = 20

# In-memory rate tracking (reset on process restart, which is fine)
_rate_counts: dict[str, list[float]] = {}


def _check_rate_limit(tenant_id: str) -> bool:
    """Return True if under rate limit."""
    import time

    now = time.time()
    cutoff = now - 3600
    counts = _rate_counts.get(tenant_id, [])
    counts = [t for t in counts if t > cutoff]
    _rate_counts[tenant_id] = counts
    return len(counts) < RATE_LIMIT_PER_HOUR


def _record_send(tenant_id: str) -> None:
    import time

    _rate_counts.setdefault(tenant_id, []).append(time.time())


class SendToUserSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=8192)
    parse_mode = serializers.ChoiceField(
        choices=["Markdown", "HTML", "plain"],
        default="Markdown",
        required=False,
    )


class CronDeliveryView(APIView):
    """Send a message to the tenant's user via Telegram or LINE.

    Auth: X-NBHD-Internal-Key + X-NBHD-Tenant-Id headers.
    Called by tenant OpenClaw containers (cron jobs, proactive messages).

    Routes to the user's preferred channel (or whichever is linked).
    Existing Telegram-only users are unaffected.
    """

    authentication_classes = []
    permission_classes = []

    def post(self, request, tenant_id):
        # Auth
        try:
            validate_internal_runtime_request(
                provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
                provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
                expected_tenant_id=str(tenant_id),
            )
        except InternalAuthError as exc:
            return Response(
                {"error": "internal_auth_failed", "detail": str(exc)},
                status=http_status.HTTP_401_UNAUTHORIZED,
            )

        # Resolve tenant
        tenant = Tenant.objects.filter(id=tenant_id).select_related("user").first()
        if tenant is None:
            return Response({"error": "tenant_not_found"}, status=http_status.HTTP_404_NOT_FOUND)

        # Block delivery for suspended/inactive tenants (trial expired, payment lapsed)
        if tenant.status != Tenant.Status.ACTIVE:
            logger.info(
                "Cron delivery blocked: tenant %s status=%s (not active)",
                tenant_id,
                tenant.status,
            )
            # Return 200 to prevent QStash/cron retries — this is expected, not an error
            return Response(
                {
                    "status": "blocked",
                    "reason": "tenant_not_active",
                    "tenant_status": tenant.status,
                }
            )

        # Determine channel
        channel = self._resolve_channel(tenant.user)
        if channel is None:
            return Response(
                {
                    "error": "no_channel_linked",
                    "detail": "User has not linked Telegram or LINE.",
                },
                status=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # Rate limit
        tid = str(tenant_id)
        if not _check_rate_limit(tid):
            return Response(
                {"error": "rate_limited", "detail": "Max 20 messages per hour."},
                status=http_status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Validate payload
        serializer = SendToUserSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=http_status.HTTP_400_BAD_REQUEST)

        message_text = serializer.validated_data["message"]
        parse_mode = serializer.validated_data.get("parse_mode", "Markdown")

        # Rehydrate PII placeholders before sending to user
        entity_map = tenant.pii_entity_map
        if entity_map:
            from apps.pii.redactor import rehydrate_text

            message_text = rehydrate_text(message_text, entity_map)

        # Route to appropriate channel
        if channel == "line":
            return self._send_via_line(
                tenant_id=tid,
                line_user_id=tenant.user.line_user_id,
                message_text=message_text,
            )
        else:
            return self._send_via_telegram(
                tenant_id=tid,
                chat_id=tenant.user.telegram_chat_id,
                message_text=message_text,
                parse_mode=parse_mode,
            )

    def _resolve_channel(self, user) -> str | None:
        """Determine which channel to use for outbound messages.

        Respects the user's preferred_channel when that channel is linked.
        Falls back to whichever channel is available.
        Returns None if no channel is linked.
        """
        preferred = getattr(user, "preferred_channel", "telegram")
        line_user_id = getattr(user, "line_user_id", None)
        telegram_chat_id = getattr(user, "telegram_chat_id", None)

        # Honour preference if that channel is linked
        if preferred == "line" and line_user_id:
            return "line"
        if preferred == "telegram" and telegram_chat_id:
            return "telegram"

        # Fallback: whichever is linked
        if line_user_id:
            return "line"
        if telegram_chat_id:
            return "telegram"

        return None

    def _send_via_telegram(
        self,
        *,
        tenant_id: str,
        chat_id: int,
        message_text: str,
        parse_mode: str,
    ) -> Response:
        """Send via Telegram Bot API."""
        bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
        if not bot_token:
            logger.error("TELEGRAM_BOT_TOKEN not configured for cron delivery")
            return Response(
                {"error": "telegram_not_configured"},
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        api_base = f"https://api.telegram.org/bot{bot_token}"
        sent_count = 0

        try:
            chunks = _split_message(message_text)

            with httpx.Client(timeout=10) as http:
                for chunk in chunks:
                    payload: dict = {"chat_id": chat_id, "text": chunk}
                    if parse_mode and parse_mode != "plain":
                        payload["parse_mode"] = parse_mode

                    resp = http.post(f"{api_base}/sendMessage", json=payload)

                    if resp.status_code == 400 and parse_mode == "Markdown":
                        payload.pop("parse_mode", None)
                        resp = http.post(f"{api_base}/sendMessage", json=payload)

                    if not resp.is_success:
                        logger.warning(
                            "Cron delivery sendMessage failed (%s): %s",
                            resp.status_code,
                            resp.text[:200],
                        )
                        return Response(
                            {"error": "telegram_send_failed", "detail": resp.text[:200]},
                            status=http_status.HTTP_502_BAD_GATEWAY,
                        )
                    sent_count += 1

        except httpx.HTTPError as exc:
            logger.exception("Cron delivery Telegram HTTP error for tenant %s", tenant_id)
            return Response(
                {"error": "telegram_send_failed", "detail": str(exc)[:200]},
                status=http_status.HTTP_502_BAD_GATEWAY,
            )

        _record_send(tenant_id)
        logger.info(
            "Cron delivery (telegram): tenant=%s chat_id=%s chunks=%d",
            tenant_id,
            chat_id,
            sent_count,
        )
        return Response({"status": "sent", "channel": "telegram", "chunks": sent_count})

    def _send_via_line(
        self,
        *,
        tenant_id: str,
        line_user_id: str,
        message_text: str,
    ) -> Response:
        """Send via LINE Push Message API with branded Flex messages."""
        access_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "")
        if not access_token:
            logger.error("LINE_CHANNEL_ACCESS_TOKEN not configured for cron delivery")
            return Response(
                {"error": "line_not_configured"},
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        import re

        from apps.router.line_flex import (
            attach_quick_reply,
            build_flex_bubble,
            extract_quick_reply_buttons,
        )
        from apps.router.line_webhook import _convert_tables, _strip_markdown

        # Extract quick reply buttons before processing
        clean_text, quick_reply_items = extract_quick_reply_buttons(message_text)

        # Pre-process: convert tables and strip code blocks
        clean_text = _convert_tables(clean_text)
        clean_text = re.sub(r"```[^\n]*\n(.*?)```", r"\1", clean_text, flags=re.DOTALL)

        # Build Flex message
        try:
            flex_msg = build_flex_bubble(clean_text)
            if quick_reply_items:
                flex_msg = attach_quick_reply(flex_msg, quick_reply_items)
            messages = [flex_msg]
        except Exception:
            logger.debug("Cron Flex build failed, falling back to plain text", exc_info=True)
            plain = _strip_markdown(clean_text)
            plain = re.sub(r"\[\[button:[^\]]+\]\]", "", plain)
            plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
            chunks = _split_message(plain, max_len=5000)
            messages = [{"type": "text", "text": c} for c in chunks[:5]]

        sent_count = 0
        try:
            with httpx.Client(timeout=10) as http:
                # Send in batches of 5 (LINE limit)
                for i in range(0, len(messages), 5):
                    batch = messages[i : i + 5]
                    resp = http.post(
                        "https://api.line.me/v2/bot/message/push",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "application/json",
                        },
                        json={"to": line_user_id, "messages": batch},
                    )
                    if not resp.is_success:
                        logger.warning(
                            "Cron delivery LINE push failed (%s): %s",
                            resp.status_code,
                            resp.text[:200],
                        )
                        return Response(
                            {"error": "line_send_failed", "detail": resp.text[:200]},
                            status=http_status.HTTP_502_BAD_GATEWAY,
                        )
                    sent_count += len(batch)

        except httpx.HTTPError as exc:
            logger.exception("Cron delivery LINE HTTP error for tenant %s", tenant_id)
            return Response(
                {"error": "line_send_failed", "detail": str(exc)[:200]},
                status=http_status.HTTP_502_BAD_GATEWAY,
            )

        _record_send(tenant_id)
        logger.info(
            "Cron delivery (line): tenant=%s line_user=%s chunks=%d",
            tenant_id,
            line_user_id[:8] if line_user_id else "?",
            sent_count,
        )
        return Response({"status": "sent", "channel": "line", "chunks": sent_count})


def _split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split a long message into chunks for Telegram."""
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
