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


def resolve_user_channel(user) -> str | None:
    """Determine which channel to use for outbound / proactive messages to ``user``.

    Order (MJ direction — keep Telegram/LINE, but push toward the app when it's
    installed):

    1. iOS device token registered → ``"app"``. Proactive content lands in the
       app feed as the PRIMARY surface: the APNs push + the ``?since=`` feed row
       (both produced by ``record_proactive_outbound``) ARE the delivery, so the
       same content no longer ALSO arrives in Telegram/LINE for token-holders.
    2. else Telegram linked → ``"telegram"``.
    3. else LINE linked → ``"line"``.
    4. else ``None`` (no delivery surface at all).

    Telegram-before-LINE in the messaging fallback (mirroring
    ``_resolve_gate_channel``) preserves prior behavior for the only both-linked
    cohort in production. The old resolver honoured ``preferred_channel``
    (universally the "telegram" schema default), so a user with BOTH channels
    linked has always received proactive/outbound messages on Telegram — a
    line-first fallback would silently move their delivery surface to LINE and
    split it from where their interactive gates land.

    ``preferred_channel`` is deliberately NOT honoured. In production all rows
    carry the schema default ``"telegram"`` (nobody ever chose it — the frontend
    hook is dead code and iOS never shipped the control), so reading it would
    honour noise, not intent. The column is left in place but vestigial; see the
    PR description.

    Linked Telegram/LINE users WITHOUT the app are unaffected — they keep full
    two-way delivery via their linked channel. Only token-holders flip from
    telegram/line to the app.

    Module-level so backend proactive senders (e.g. Core notify-on-ready) route
    identically to ``CronDeliveryView`` without duplicating the logic.
    """
    # 1. Prefer the app whenever an iOS device is registered.
    from apps.router.models import DeviceToken

    if DeviceToken.objects.filter(user=user).exists():
        return "app"

    # 2/3. No device — fall back to whichever messaging channel is linked so
    # linked users without the app keep working. Telegram before LINE so a
    # both-linked user keeps the delivery surface they've always had (see
    # docstring); line-only users still resolve to LINE.
    line_user_id = getattr(user, "line_user_id", None)
    telegram_chat_id = getattr(user, "telegram_chat_id", None)
    if telegram_chat_id:
        return "telegram"
    if line_user_id:
        return "line"

    return None


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
                    "detail": "User has no Telegram/LINE link and no registered device.",
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

        # As authored by the agent — PII-placeholder space ([PERSON_1]). Retained
        # so the at-rest copies (ProactiveOutbound.message_text, the LINE
        # quote-reply excerpt) are stored placeholder-space; only the copy
        # actually sent to the user is rehydrated.
        placeholder_message_text = serializer.validated_data["message"]
        parse_mode = serializer.validated_data.get("parse_mode", "Markdown")

        # Quick-reply buttons aren't wired for cron/proactive sends (no
        # ProactiveOutbound column, no iOS UI for it this cycle — see PR
        # description). Still strip the marker here so it can never leak as
        # raw text on ANY channel if an agent emits it on a proactive send.
        from apps.router.journal_link import extract_journal_link
        from apps.router.quick_replies import extract_quick_replies

        placeholder_message_text, _quick_replies = extract_quick_replies(
            placeholder_message_text, tenant_id=tenant.id, channel=f"cron_{channel}"
        )
        # The journal deep-link chip, unlike quick-replies, IS persisted for
        # proactive sends: it rides the ProactiveOutbound row and surfaces in the
        # iOS ?since= feed (cross-channel), so a Telegram-delivered morning report
        # still gets a tappable "View in Journal" chip when its author opens the
        # app. Strip the marker from the text sent on every channel (no chip
        # transport on Telegram/LINE), but keep the parsed link for the row below.
        placeholder_message_text, journal_link = extract_journal_link(
            placeholder_message_text, tenant_id=tenant.id, channel=f"cron_{channel}"
        )

        # Rehydrate PII placeholders before sending to user (owner-facing egress).
        entity_map = tenant.pii_entity_map
        message_text = placeholder_message_text
        if entity_map:
            from apps.pii.redactor import rehydrate_text

            message_text = rehydrate_text(placeholder_message_text, entity_map)

        # Log-only instrumentation: ASCII chart leakage when no marker emitted.
        from apps.router.output_guards import log_ascii_chart_leak

        log_ascii_chart_leak(message_text, tenant_id=tenant.id, channel=f"cron_{channel}")

        # Route to appropriate channel
        if channel == "line":
            channel_user_id = tenant.user.line_user_id or ""
            resp = self._send_via_line(
                tenant_id=tid,
                line_user_id=channel_user_id,
                message_text=message_text,
                # Store the quote-reply excerpt in placeholder space, not the
                # rehydrated body we actually push.
                excerpt_override=placeholder_message_text,
            )
        elif channel == "app":
            # iOS-only user: there's no Telegram/LINE chat to send to. The APNs
            # push + the ?since= feed row ARE the delivery — both produced by
            # record_proactive_outbound below. Use the user id as the stable
            # per-user app identifier.
            channel_user_id = str(tenant.user_id)
            resp = Response({"status": "sent", "channel": "app"})
        else:
            channel_user_id = str(tenant.user.telegram_chat_id or "")
            resp = self._send_via_telegram(
                tenant_id=tid,
                chat_id=tenant.user.telegram_chat_id,
                message_text=message_text,
                parse_mode=parse_mode,
            )

        # Count every successful send against the per-tenant hourly cap. Done
        # here (not per-branch) so the runaway-loop throttle covers ALL channels
        # uniformly — including the app channel, whose inline 2xx Response never
        # passes through _send_via_telegram/_send_via_line.
        if 200 <= resp.status_code < 300:
            _record_send(tid)

        # Record the outbound for thread-continuity on the next inbound.
        # This is the deterministic replacement for the LLM-mediated
        # ``_phase2_sync_block`` path — see apps.router.proactive_context.
        if 200 <= resp.status_code < 300 and channel_user_id:
            from apps.router.proactive_context import record_proactive_outbound

            record_proactive_outbound(
                tenant=tenant,
                channel=channel,
                channel_user_id=channel_user_id,
                # Placeholder-space at rest; record_proactive_outbound rehydrates
                # only for the owner-facing iOS push it fires.
                message_text=placeholder_message_text,
                job_name=request.headers.get("X-NBHD-Job-Name", ""),
                # Parsed "View in Journal" deep-link (placeholder-space title);
                # the ?since= feed rehydrates + renders it as a chip. None when
                # the send carried no marker.
                journal_link=journal_link,
            )

        return resp

    def _resolve_channel(self, user) -> str | None:
        """Determine which channel to use for outbound messages.

        Thin wrapper over the module-level ``resolve_user_channel`` (shared with
        backend proactive senders).
        """
        return resolve_user_channel(user)

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

        from apps.router.telegram_format import (
            markdown_to_plaintext,
            render_telegram_html,
            strip_telegram_html,
        )

        # Render the assistant's markdown into Telegram HTML (bold headings,
        # aligned tables, anchors — no visible markdown). ``parse_mode="plain"``
        # opts out and sends markdown-stripped text.
        if parse_mode == "plain":
            chunks = _split_message(markdown_to_plaintext(message_text))
            html_mode = False
        else:
            chunks = render_telegram_html(message_text) or _split_message(message_text)
            html_mode = True

        try:
            with httpx.Client(timeout=10) as http:
                for chunk in chunks:
                    payload: dict = {"chat_id": chat_id, "text": chunk}
                    if html_mode:
                        payload["parse_mode"] = "HTML"

                    resp = http.post(f"{api_base}/sendMessage", json=payload)

                    if resp.status_code == 400 and html_mode:
                        # HTML rejected — retry as tag-free text (no markdown).
                        resp = http.post(
                            f"{api_base}/sendMessage",
                            json={"chat_id": chat_id, "text": strip_telegram_html(chunk)},
                        )

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
        excerpt_override: str | None = None,
    ) -> Response:
        """Send via LINE Push Message API with branded Flex messages.

        ``message_text`` is the rehydrated body sent to the user; when
        ``excerpt_override`` is given it is the placeholder-space copy stored as
        the quote-reply excerpt (so ``LineOutboundMessage.text_excerpt`` holds no
        real names).
        """
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

        from apps.router.line_webhook import _record_line_outbound
        from apps.tenants.models import Tenant

        tenant_obj = Tenant.objects.filter(id=tenant_id).first()

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
                        # Trip the fleet-wide quota gate if this is the
                        # monthly-cap 429 (vs a transient rate-limit).
                        from apps.router.line_webhook import _maybe_trip_monthly_quota

                        _maybe_trip_monthly_quota(resp.status_code, resp.text)
                        return Response(
                            {"error": "line_send_failed", "detail": resp.text[:200]},
                            status=http_status.HTTP_502_BAD_GATEWAY,
                        )
                    if tenant_obj:
                        try:
                            sent_messages = (resp.json() or {}).get("sentMessages") or []
                        except Exception:
                            sent_messages = []
                        _record_line_outbound(
                            tenant_obj, line_user_id, sent_messages, batch, excerpt_override=excerpt_override
                        )
                    sent_count += len(batch)

        except httpx.HTTPError as exc:
            logger.exception("Cron delivery LINE HTTP error for tenant %s", tenant_id)
            return Response(
                {"error": "line_send_failed", "detail": str(exc)[:200]},
                status=http_status.HTTP_502_BAD_GATEWAY,
            )

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
