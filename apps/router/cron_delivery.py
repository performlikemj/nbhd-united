"""Endpoint for tenant agents to send messages to users via Django poller.

Used by cron jobs and proactive agent actions. Routes messages through
the central Telegram bot instead of requiring a bot token in each container.
"""
from __future__ import annotations

import logging

import httpx
from django.conf import settings
from rest_framework import serializers, status as http_status
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
    """Send a message to the tenant's user via Telegram.

    Auth: X-NBHD-Internal-Key + X-NBHD-Tenant-Id headers.
    Called by tenant OpenClaw containers (cron jobs, proactive messages).
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

        chat_id = getattr(tenant.user, "telegram_chat_id", None)
        if not chat_id:
            return Response(
                {"error": "no_telegram_chat", "detail": "User has not linked Telegram."},
                status=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # Rate limit
        tid = str(tenant_id)
        if not _check_rate_limit(tid):
            return Response(
                {"error": "rate_limited", "detail": "Max 20 messages per hour."},
                status=http_status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Validate
        serializer = SendToUserSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=http_status.HTTP_400_BAD_REQUEST)

        message_text = serializer.validated_data["message"]
        parse_mode = serializer.validated_data.get("parse_mode", "Markdown")

        # Send via Telegram Bot API
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
            # Split long messages (Telegram 4096 char limit)
            chunks = _split_message(message_text)

            with httpx.Client(timeout=10) as http:
                for chunk in chunks:
                    payload = {"chat_id": chat_id, "text": chunk}
                    if parse_mode and parse_mode != "plain":
                        payload["parse_mode"] = parse_mode

                    resp = http.post(f"{api_base}/sendMessage", json=payload)

                    if resp.status_code == 400 and parse_mode == "Markdown":
                        # Markdown rejected — retry as plain text
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
            logger.exception("Cron delivery HTTP error for tenant %s", tenant_id)
            return Response(
                {"error": "telegram_send_failed", "detail": str(exc)[:200]},
                status=http_status.HTTP_502_BAD_GATEWAY,
            )

        _record_send(tid)
        logger.info("Cron delivery: tenant=%s chat_id=%s chunks=%d", tenant_id, chat_id, sent_count)

        return Response(
            {"status": "sent", "chunks": sent_count},
            status=http_status.HTTP_200_OK,
        )


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
