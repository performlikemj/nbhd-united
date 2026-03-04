"""LINE Messaging API webhook receiver.

POST /api/v1/line/webhook/

- Verifies LINE signature (HMAC-SHA256)
- Parses events from the request body
- Handles follow, message (text), and unfollow events
- Processes AI forwarding asynchronously (LINE requires 200 within 1 second)
- Uses LINE Push Message API for responses (reply_token expires too fast)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
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
from apps.tenants.models import Tenant, User

logger = logging.getLogger(__name__)

LINE_API_BASE = "https://api.line.me/v2/bot"
CHAT_COMPLETIONS_TIMEOUT = 120.0  # generous timeout for AI response
LOADING_SECONDS = 20  # loading animation duration (auto-clears on response)


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
    """
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
    return text


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
            _send_line_text(
                line_user_id,
                f"Welcome back, {tenant.user.display_name}! 👋\n\n"
                "Your LINE account is already connected. You can start chatting!",
            )
            return

        _send_line_text(
            line_user_id,
            "👋 Welcome to Neighborhood United!\n\n"
            "To connect your account:\n"
            f"1. Sign up at {frontend_url}\n"
            "2. Go to Settings → Connect LINE\n"
            "3. Tap the link or scan the QR code\n\n"
            "Once connected, you can chat with your AI assistant right here!",
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
        """Handle incoming text message."""
        message = event.get("message", {})
        msg_type = message.get("type", "")
        reply_token = event.get("replyToken")

        if msg_type != "text":
            line_user_id = event.get("source", {}).get("userId", "")
            if line_user_id:
                _send_line_text(
                    line_user_id,
                    "I can only process text messages for now. Please send text!",
                )
            return

        text = message.get("text", "").strip()
        line_user_id = event.get("source", {}).get("userId", "")

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
            _send_line_text(
                line_user_id,
                "I don't recognize your account yet.\n\n"
                f"Sign up at {frontend_url} and connect LINE "
                "from your Settings page to get started!",
            )
            return

        # Provisioning tenant
        if tenant.status in (Tenant.Status.PENDING, Tenant.Status.PROVISIONING):
            _send_line_text(
                line_user_id,
                "Your assistant is waking up! 🌅 This usually takes about a minute. "
                "Please try again shortly!",
            )
            return

        # Suspended tenant
        frontend_url = getattr(
            settings, "FRONTEND_URL", "https://neighborhoodunited.org"
        ).rstrip("/")
        if (
            tenant.status == Tenant.Status.SUSPENDED
            and not tenant.is_trial
            and not bool(tenant.stripe_subscription_id)
        ):
            _send_line_text(
                line_user_id,
                f"Your free trial has ended. Subscribe to continue: "
                f"{frontend_url}/settings/billing",
            )
            return

        # Budget check
        if not check_budget(tenant):
            _send_line_text(
                line_user_id,
                f"You've hit your monthly token quota. "
                f"Open Billing to upgrade at {frontend_url}/billing.",
            )
            return

        # Show loading animation while LLM processes
        _show_loading(line_user_id)

        # Update last_message_at
        Tenant.objects.filter(id=tenant.id).update(last_message_at=timezone.now())

        # Forward to container (pass reply_token for free Reply API)
        self._forward_to_container(line_user_id, tenant, text, reply_token=reply_token)

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
    ) -> None:
        """Forward message to tenant's OpenClaw container and relay response via LINE.

        Uses Flex Messages for structured content, quick replies for buttons,
        and Reply API (free) with Push fallback.
        """
        from apps.router.line_flex import (
            attach_quick_reply,
            build_flex_bubble,
            extract_quick_reply_buttons,
            should_use_flex,
        )

        if not tenant.container_fqdn:
            _send_line_text(
                line_user_id,
                "Your assistant is being set up. Please try again in a minute!",
            )
            return

        url = f"https://{tenant.container_fqdn}/v1/chat/completions"
        user_tz = tenant.user.timezone or "UTC"
        gateway_token = getattr(settings, "NBHD_INTERNAL_API_KEY", "").strip()

        try:
            resp = httpx.post(
                url,
                json={
                    "model": "openclaw",
                    "messages": [{"role": "user", "content": message_text}],
                    "user": line_user_id,
                },
                headers={
                    "Authorization": f"Bearer {gateway_token}",
                    "X-User-Timezone": user_tz,
                    "X-Line-User-Id": line_user_id,
                },
                timeout=CHAT_COMPLETIONS_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json()
        except httpx.TimeoutException:
            logger.warning(
                "Timeout forwarding to %s for line_user_id=%s",
                tenant.container_fqdn,
                line_user_id,
            )
            _send_line_text(
                line_user_id,
                "⏱️ That took longer than expected. Could you send it again?",
            )
            return
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response else 0
            logger.error(
                "LINE FWD_FAIL %s HTTP %s", tenant.container_fqdn, status_code
            )
            if status_code in (502, 503):
                _send_line_text(
                    line_user_id,
                    "⏳ I'm restarting — please try again in about a minute!",
                )
            else:
                _send_line_text(
                    line_user_id,
                    "Something went wrong. Please try again.",
                )
            return
        except httpx.HTTPError as e:
            logger.error("LINE forward error: %s", e)
            _send_line_text(
                line_user_id,
                "⏳ I'm restarting — please try again in about a minute!",
            )
            return

        # Extract AI response
        ai_text = self._extract_ai_response(result)
        if ai_text:
            # Remove MEDIA markers (images not supported yet)
            clean_text = re.sub(r"MEDIA:\S+", "", ai_text).strip()

            # Extract quick reply buttons before stripping markdown
            clean_text, quick_reply_items = extract_quick_reply_buttons(clean_text)

            # Decide: Flex or plain text
            messages: list[dict] = []
            try:
                if should_use_flex(clean_text):
                    flex_msg = build_flex_bubble(clean_text)
                    messages = [flex_msg]
                else:
                    # Plain text with markdown stripped
                    plain = _strip_markdown(clean_text)
                    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
                    if plain:
                        chunks = _split_message(plain, max_len=5000)
                        messages = [{"type": "text", "text": c} for c in chunks[:5]]
            except Exception:
                # Flex construction failed — fall back to plain text
                logger.debug("Flex build failed, falling back to plain text", exc_info=True)
                plain = _strip_markdown(clean_text)
                plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
                if plain:
                    chunks = _split_message(plain, max_len=5000)
                    messages = [{"type": "text", "text": c} for c in chunks[:5]]

            # Attach quick replies to the last message
            if messages and quick_reply_items:
                messages[-1] = attach_quick_reply(messages[-1], quick_reply_items)

            # Send via Reply API (free) with Push fallback
            if messages:
                _send_line_messages(line_user_id, messages, reply_token=reply_token)

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

        # Forward postback data as a message to the agent
        self._forward_to_container(
            line_user_id,
            tenant,
            f'[User tapped button: "{data}"]',
        )

    @staticmethod
    def _extract_ai_response(result: dict) -> str | None:
        """Extract AI response text from chat completions response."""
        try:
            choices = result.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content")
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
