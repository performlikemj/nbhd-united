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
from typing import Any

import httpx
from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.billing.services import check_budget, record_usage
from apps.router.error_messages import error_msg
from apps.router.line_flex import (
    attach_quick_reply,
    build_flex_bubble,
    build_short_bubble,
    build_status_bubble,
    extract_quick_reply_buttons,
    should_use_flex,
    telegram_keyboard_to_quick_reply,
)
from apps.tenants.models import Tenant, User

logger = logging.getLogger(__name__)

LINE_API_BASE = "https://api.line.me/v2/bot"
LINE_CONTENT_API = "https://api-data.line.me/v2/bot/message"
CHAT_COMPLETIONS_TIMEOUT = 120.0  # generous timeout for AI response
VOICE_CHAT_TIMEOUT = 180.0  # extra budget for voice (cold-start after Whisper)
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
    openai_key = getattr(settings, "OPENAI_API_KEY", "") or os.environ.get(
        "OPENAI_API_KEY", ""
    )
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


def _send_line_reply(reply_token: str, messages: list[dict]) -> bool:
    """Send messages via LINE Reply Message API (free, unlimited).

    Returns True on success, False if token expired or other failure.
    """
    access_token = _get_access_token()
    if not access_token or not reply_token:
        return False
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
            return True
        # 400 with "Invalid reply token" = expired
        logger.debug("LINE reply failed (%s): %s", resp.status_code, resp.text[:200])
        return False
    except Exception:
        logger.debug("LINE reply exception", exc_info=True)
        return False


def _send_line_messages(
    line_user_id: str,
    messages: list[dict],
    reply_token: str | None = None,
) -> bool:
    """Send messages, preferring Reply API (free) with Push fallback.

    Tries reply_token first. If it fails (expired/missing), falls back to Push.
    """
    if reply_token and _send_line_reply(reply_token, messages):
        return True
    return _send_line_push(line_user_id, messages)


def _send_line_push(line_user_id: str, messages: list[dict]) -> bool:
    """Send messages via LINE Push Message API.

    Returns True on success.
    """
    access_token = _get_access_token()
    if not access_token:
        logger.error("LINE_CHANNEL_ACCESS_TOKEN not configured")
        return False

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
            return False
        return True
    except Exception:
        logger.exception("Failed to send LINE push message to %s", line_user_id)
        return False


def _send_line_text(line_user_id: str, text: str) -> bool:
    """Send a single text message via LINE Push API."""
    # LINE max message length is 5000 chars
    if len(text) > 5000:
        # Split into chunks
        chunks = _split_message(text, max_len=5000)
        # LINE allows max 5 messages per push
        for i in range(0, len(chunks), 5):
            batch = [{"type": "text", "text": c} for c in chunks[i:i + 5]]
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
    text = re.sub(r'```[^\n]*\n(.*?)```', r'\1', text, flags=re.DOTALL)
    # Remove bold markers
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    # Remove italic markers
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    # Convert markdown links to plain URLs
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1: \2', text)
    # Remove inline code markers
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Remove heading markers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
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
        if cells and all(re.match(r'^[-:]+$', c) for c in cells):
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
        # LINE requires 200 within 1 second — never block here
        for event in events:
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

        frontend_url = getattr(
            settings, "FRONTEND_URL", "https://neighborhoodunited.org"
        ).rstrip("/")

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

        # Audio/voice messages — transcribe via Whisper
        if msg_type == "audio":
            if not line_user_id:
                return
            message_id = message.get("id")
            if not message_id:
                return
            logger.info(
                "LINE audio received: message_id=%s from %s",
                message_id, line_user_id,
            )
            _show_loading(line_user_id)
            transcript = _transcribe_line_audio(message_id)
            if transcript:
                logger.info(
                    "LINE audio transcribed: %d chars from message_id=%s",
                    len(transcript), message_id,
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
                        "Sorry, I couldn't transcribe that audio. "
                        "Please try again or send a text message.",
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
                package_id, sticker_id, sticker_resource, keywords,
            )
        elif msg_type == "text":
            text = message.get("text", "").strip()
        else:
            # Unsupported message types (image, video, location, etc.)
            if line_user_id:
                _send_line_flex(
                    line_user_id,
                    build_status_bubble(
                        "I can process text, voice, and stickers. "
                        "Please send one of those!",
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

        # Resolve tenant
        tenant = _resolve_tenant_by_line_user_id(line_user_id)

        if not tenant:
            frontend_url = getattr(
                settings, "FRONTEND_URL", "https://neighborhoodunited.org"
            ).rstrip("/")
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
        frontend_url = getattr(
            settings, "FRONTEND_URL", "https://neighborhoodunited.org"
        ).rstrip("/")
        if (
            tenant.status == Tenant.Status.SUSPENDED
            and not tenant.is_trial
            and not bool(tenant.stripe_subscription_id)
        ):
            lang = tenant.user.language or "en"
            _send_line_flex(
                line_user_id,
                build_status_bubble(
                    error_msg(
                        lang, "suspended",
                        billing_url=f"{frontend_url}/settings/billing",
                    ),
                    tone="warning",
                ),
            )
            return

        # Hibernated tenant — buffer message and wake container
        from apps.router.wake_on_message import handle_hibernated_message

        wake_result = handle_hibernated_message(tenant, "line", event, text)
        if wake_result is True:
            lang = tenant.user.language or "en"
            _send_line_flex(
                line_user_id,
                build_short_bubble(error_msg(lang, "hibernation_waking")),
            )
            return
        elif wake_result is False:
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
                    build_short_bubble(
                        "I'll be ready for stickers and voice soon! "
                        "For now, please type your answer."
                    ),
                )
                return
            # LINE has no language_code — always ask language question
            onboarding_reply = get_onboarding_response(
                tenant, text, telegram_lang=""
            )
            if onboarding_reply is not None:
                self._send_onboarding_reply(line_user_id, onboarding_reply)
                return

        # Budget check
        budget_reason = check_budget(tenant)
        if budget_reason:
            lang = tenant.user.language or "en"
            if budget_reason == "global":
                msg_key = "budget_unavailable"
                kwargs: dict[str, str] = {}
            else:
                msg_key = (
                    "budget_exhausted_trial" if tenant.is_trial else "budget_exhausted_paid"
                )
                kwargs = {
                    "plus_message": "",
                    "billing_url": f"{frontend_url}/billing",
                }
            _send_line_flex(
                line_user_id,
                build_status_bubble(error_msg(lang, msg_key, **kwargs), tone="warning"),
            )
            return

        # Show loading animation while LLM processes
        _show_loading(line_user_id)

        # Update last_message_at
        Tenant.objects.filter(id=tenant.id).update(last_message_at=timezone.now())

        # Forward to container (pass reply_token for free Reply API)
        self._forward_to_container(
            line_user_id, tenant, text,
            reply_token=reply_token,
            is_voice=msg_type == "audio",
        )

    def _send_onboarding_reply(self, line_user_id: str, reply) -> None:
        """Render an OnboardingReply as a LINE Flex message with optional Quick Reply buttons."""
        msg = build_short_bubble(reply.text)
        if reply.keyboard:
            items = telegram_keyboard_to_quick_reply(reply.keyboard)
            msg = attach_quick_reply(msg, items)
        _send_line_push(line_user_id, [msg])

    def _handle_onboarding_postback(
        self, tenant: Tenant, line_user_id: str, data: str
    ) -> None:
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
    ) -> None:
        """Forward message to tenant's OpenClaw container and relay response via LINE.

        Uses Flex Messages for structured content, quick replies for buttons,
        and Reply API (free) with Push fallback.
        """
        if not tenant.container_fqdn or tenant.status == "provisioning":
            _send_line_flex(
                line_user_id,
                build_short_bubble(
                    "Your assistant is being set up — this usually takes about a minute. "
                    "I'll be ready for you shortly! 🌱",
                ),
            )
            return

        url = f"https://{tenant.container_fqdn}/v1/chat/completions"
        user_tz = tenant.user.timezone or "UTC"
        gateway_token = getattr(settings, "NBHD_INTERNAL_API_KEY", "").strip()

        # Workspace routing — returns base line_user_id if tenant has no workspaces
        from apps.router.workspace_routing import (
            build_transition_marker,
            build_workspace_context_marker,
            resolve_workspace_routing,
            update_active_workspace,
        )
        user_param, workspace, transitioned = resolve_workspace_routing(
            tenant, line_user_id, message_text,
        )
        if transitioned and workspace is not None:
            message_text = build_transition_marker(workspace) + message_text
        # Always inject the active workspace marker so the agent knows which
        # workspace it's in (handles UI switches that bypass routing entirely).
        if workspace is not None:
            message_text = build_workspace_context_marker(workspace) + message_text
        if workspace is not None:
            update_active_workspace(tenant, workspace)

        try:
            resp = httpx.post(
                url,
                json={
                    "model": "openclaw",
                    "messages": [{"role": "user", "content": message_text}],
                    "user": user_param,
                },
                headers={
                    "Authorization": f"Bearer {gateway_token}",
                    "X-User-Timezone": user_tz,
                    "X-Line-User-Id": line_user_id,
                    "X-Channel": "line",
                },
                timeout=VOICE_CHAT_TIMEOUT if is_voice else CHAT_COMPLETIONS_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json()
        except httpx.TimeoutException:
            logger.warning(
                "Timeout forwarding to %s for line_user_id=%s",
                tenant.container_fqdn,
                line_user_id,
            )
            _send_line_flex(
                line_user_id,
                build_status_bubble(
                    "That took longer than expected. Could you send it again?",
                    tone="warning",
                ),
            )
            return
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response else 0
            logger.error(
                "LINE FWD_FAIL %s HTTP %s", tenant.container_fqdn, status_code
            )
            if status_code in (502, 503):
                # Check if this is a brand new tenant still starting up
                tenant.refresh_from_db(fields=["status"])
                if tenant.status == "provisioning":
                    _send_line_flex(
                        line_user_id,
                        build_short_bubble(
                            "Your assistant is almost ready — just finishing setup. "
                            "Try again in about a minute! 🌱",
                        ),
                    )
                else:
                    _send_line_flex(
                        line_user_id,
                        build_status_bubble(
                            "I'm restarting \u2014 please try again in about a minute!",
                            tone="warning",
                        ),
                    )
            else:
                _send_line_flex(
                    line_user_id,
                    build_status_bubble(
                        "Something went wrong. Please try again.",
                        tone="error",
                    ),
                )
            return
        except httpx.HTTPError as e:
            logger.error("LINE forward error: %s", e)
            tenant.refresh_from_db(fields=["status"])
            if tenant.status == "provisioning":
                _send_line_flex(
                    line_user_id,
                    build_short_bubble(
                        "Your assistant is almost ready — just finishing setup. "
                        "Try again in about a minute! 🌱",
                    ),
                )
            else:
                _send_line_flex(
                    line_user_id,
                    build_status_bubble(
                        "I'm restarting \u2014 please try again in about a minute!",
                        tone="warning",
                    ),
                )
            return

        # Extract AI response — retry once on empty
        ai_text = self._extract_ai_response(result)
        if not ai_text:
            logger.warning(
                "Empty AI response from %s: keys=%s, choices=%r",
                tenant.container_fqdn,
                list(result.keys()),
                result.get("choices", [])[:1],
            )
            logger.warning(
                "Empty response from container %s, retrying once",
                tenant.container_fqdn,
            )
            try:
                retry_resp = httpx.post(
                    url,
                    json={
                        "model": "openclaw",
                        "messages": [{"role": "user", "content": message_text}],
                        "user": user_param,
                    },
                    headers={
                        "Authorization": f"Bearer {gateway_token}",
                        "X-User-Timezone": user_tz,
                        "X-Line-User-Id": line_user_id,
                        "X-Channel": "line",
                    },
                    timeout=CHAT_COMPLETIONS_TIMEOUT,
                )
                retry_resp.raise_for_status()
                result = retry_resp.json()
                ai_text = self._extract_ai_response(result)
            except Exception:
                logger.warning("Retry also failed for %s", tenant.container_fqdn)

        if not ai_text:
            logger.error(
                "No response after retry from container %s for line_user_id=%s: "
                "keys=%s, choices=%r",
                tenant.container_fqdn,
                line_user_id,
                list(result.keys()),
                result.get("choices", [])[:1],
            )
            _send_line_flex(
                line_user_id,
                build_short_bubble(
                    "Sorry, I couldn't come up with a response. "
                    "Could you try saying that again?",
                ),
            )
            self._record_usage(tenant, result)
            return

        if ai_text:
            # Rehydrate PII placeholders before sending to user
            entity_map = getattr(tenant, "pii_entity_map", None)
            if entity_map:
                from apps.pii.redactor import rehydrate_text
                ai_text = rehydrate_text(ai_text, entity_map)

            try:
                # Render [[chart:type]] markers into images
                image_messages: list[dict] = []
                chart_pattern = re.compile(r'\[\[chart:(\w+)(?:\|(.+?))?\]\]')
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
                            image_messages.append({
                                "type": "image",
                                "originalContentUrl": chart_url,
                                "previewImageUrl": chart_url,
                            })
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

                # Send via Reply API (free) with Push fallback
                if messages:
                    sent = _send_line_messages(line_user_id, messages, reply_token=reply_token)
                    if not sent:
                        logger.error(
                            "LINE response delivery failed for %s (container=%s)",
                            line_user_id, tenant.container_fqdn,
                        )
                        # Retry with plain text as emergency fallback
                        fallback_text = _strip_markdown(ai_text)
                        _send_line_text(line_user_id, fallback_text[:5000])
            except Exception:
                logger.exception(
                    "Error building LINE response for %s", line_user_id,
                )
                # Emergency fallback — strip markdown before sending
                fallback_text = _strip_markdown(ai_text)
                _send_line_text(line_user_id, fallback_text[:5000])

        # Record usage
        self._record_usage(tenant, result)

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

        # Lesson approval callbacks (agent-suggested)
        if data.startswith("lesson:"):
            self._handle_lesson_postback(tenant, data)
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
        from apps.actions.models import ActionStatus, PendingAction, ActionAuditLog
        from apps.actions.messaging import update_gate_message
        from django.utils import timezone

        try:
            parts = data.split(":")
            if len(parts) != 2:
                return

            is_approve = parts[0] == "gate_approve"
            action_id = int(parts[1])

            action = PendingAction.objects.get(id=action_id, tenant=tenant)

            if action.status != ActionStatus.PENDING:
                return

            if action.is_expired:
                action.status = ActionStatus.EXPIRED
                action.save(update_fields=["status"])
                ActionAuditLog.objects.create(
                    tenant=tenant,
                    action_type=action.action_type,
                    action_payload=action.action_payload,
                    display_summary=action.display_summary,
                    result=ActionStatus.EXPIRED,
                )
                update_gate_message(action)
                return

            now = timezone.now()
            action.status = ActionStatus.APPROVED if is_approve else ActionStatus.DENIED
            action.responded_at = now
            action.save(update_fields=["status", "responded_at"])

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
        from apps.journal.models import PendingExtraction
        from apps.router.extraction_callbacks import (
            _approve_lesson,
            _approve_goal,
            _approve_task,
            _undo_lesson,
            _undo_goal,
            _undo_task,
        )
        from django.utils import timezone as tz

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
                status_text = "already added" if pending.status == PendingExtraction.Status.APPROVED else "already skipped"
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

            kind_label = {"lesson": "Saved to constellation", "goal": "Added to goals", "task": "Added to tasks"}.get(pending.kind, "Added")
            _send_line_status_bubble(tenant, f"{kind_label}: {pending.text[:80]}", tone="success")

        except Exception:
            logger.exception("Error handling extraction postback: %s", data)

    @staticmethod
    def _handle_lesson_postback(tenant, data: str) -> None:
        """Handle lesson approval/dismissal from LINE postback.

        Data format: lesson:<action>:<lesson_id>
        """
        from apps.lessons.models import Lesson
        from apps.lessons.services import process_approved_lesson
        from django.utils import timezone as tz

        try:
            parts = data.split(":")
            if len(parts) != 3:
                return

            action = parts[1]
            lesson_id = int(parts[2])

            lesson = Lesson.objects.filter(
                id=lesson_id, tenant=tenant, status="pending"
            ).first()
            if not lesson:
                return

            if action == "approve":
                lesson.status = "approved"
                lesson.approved_at = tz.now()
                lesson.save(update_fields=["status", "approved_at"])

                try:
                    process_approved_lesson(lesson)
                except Exception:
                    logger.exception("Failed to process approved lesson %s", lesson_id)

                _send_line_follow_up(tenant, f"✅ Approved: {lesson.text[:100]}")

            elif action == "dismiss":
                lesson.status = "dismissed"
                lesson.save(update_fields=["status"])
                _send_line_follow_up(tenant, f"❌ Dismissed: {lesson.text[:100]}")

        except Exception:
            logger.exception("Error handling lesson postback: %s", data)

    # Gateway error strings that should be treated as empty responses
    _GATEWAY_ERROR_STRINGS = frozenset({
        "No response from OpenClaw.",
        "No response from OpenClaw",
    })

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

        input_tokens = (
            usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
        )
        output_tokens = (
            usage.get("completion_tokens", 0)
            or usage.get("output_tokens", 0)
            or 0
        )
        model_used = result.get("model", "")

        if input_tokens or output_tokens:
            try:
                record_usage(
                    tenant=tenant,
                    event_type="message",
                    input_tokens=int(input_tokens),
                    output_tokens=int(output_tokens),
                    model_used=model_used or "",
                )
            except Exception:
                logger.exception(
                    "Failed to record usage for tenant %s", tenant.id
                )
