"""Central Telegram poller — one process polls getUpdates for the shared bot,
routes each message to the correct tenant's OpenClaw container via the
/v1/chat/completions endpoint, then sends the AI reply back to the user."""

from __future__ import annotations

import logging
import signal
import time
from typing import Any

import httpx
from django.conf import settings
from django.utils import timezone

from apps.billing.services import check_budget, record_usage
from apps.tenants.models import Tenant
from .services import (
    extract_chat_id,
    handle_start_command,
    is_rate_limited,
    resolve_tenant_by_chat_id,
    send_onboarding_link,
)

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot"
POLL_TIMEOUT = 30  # seconds for long-polling
MAX_BACKOFF = 60  # max seconds between retries on error
CHAT_COMPLETIONS_TIMEOUT = 120.0  # generous timeout for AI response


class TelegramPoller:
    """Long-polls Telegram getUpdates and routes messages to tenant containers."""

    def __init__(self) -> None:
        self.bot_token: str = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
        self.webhook_secret: str = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "").strip()
        self.offset: int = 0
        self._running = False
        self._backoff = 1
        self._http: httpx.Client | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Run the polling loop. Blocks until signalled to stop."""
        if not self.bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

        self._running = True
        self._install_signal_handlers()

        # Delete any existing webhook so getUpdates works
        self._delete_webhook()

        logger.info("Central Telegram poller starting (long-poll timeout=%ds)", POLL_TIMEOUT)

        self._http = httpx.Client(timeout=httpx.Timeout(POLL_TIMEOUT + 10, connect=10))
        try:
            while self._running:
                try:
                    updates = self._get_updates()
                    if updates:
                        self._backoff = 1  # reset on success
                        for update in updates:
                            self._process_update(update)
                    # Even an empty list is a successful poll
                    self._backoff = 1
                except httpx.TimeoutException:
                    # Normal for long-polling — just retry
                    continue
                except Exception:
                    logger.exception("Error in poll loop, backing off %ds", self._backoff)
                    time.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, MAX_BACKOFF)
        finally:
            self._http.close()
            self._http = None
            logger.info("Central Telegram poller stopped")

    def stop(self) -> None:
        """Signal the poller to stop gracefully."""
        logger.info("Shutdown requested")
        self._running = False

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._handle_signal)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        logger.info("Received signal %s", signal.Signals(signum).name)
        self.stop()

    # ------------------------------------------------------------------
    # Telegram API helpers
    # ------------------------------------------------------------------

    @property
    def _api_base(self) -> str:
        return f"{TELEGRAM_API_BASE}{self.bot_token}"

    def _delete_webhook(self) -> None:
        """Delete any existing webhook so getUpdates works."""
        try:
            resp = httpx.post(
                f"{self._api_base}/deleteWebhook",
                json={"drop_pending_updates": False},
                timeout=10,
            )
            data = resp.json()
            logger.info("deleteWebhook result: %s", data.get("description", data))
        except Exception:
            logger.exception("Failed to delete webhook")

    def _get_updates(self) -> list[dict]:
        """Long-poll for updates from Telegram."""
        assert self._http is not None
        resp = self._http.post(
            f"{self._api_base}/getUpdates",
            json={
                "timeout": POLL_TIMEOUT,
                "offset": self.offset,
                "allowed_updates": ["message", "callback_query", "edited_message"],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logger.error("getUpdates returned ok=false: %s", data)
            return []
        return data.get("result", [])

    def _send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        """Send a message via Telegram Bot API."""
        assert self._http is not None
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, **kwargs}
        try:
            resp = self._http.post(
                f"{self._api_base}/sendMessage",
                json=payload,
                timeout=10,
            )
            if not resp.is_success:
                logger.warning("sendMessage failed (%s): %s", resp.status_code, resp.text[:200])
        except Exception:
            logger.exception("Failed to send message to chat_id=%s", chat_id)

    def _answer_callback_query(self, callback_id: str, text: str) -> None:
        """Answer a Telegram callback query."""
        assert self._http is not None
        try:
            self._http.post(
                f"{self._api_base}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
                timeout=5,
            )
        except Exception:
            logger.exception("Failed to answer callback query %s", callback_id)

    def _execute_telegram_response(self, response_data: dict) -> None:
        """Execute a Telegram API method from a response dict (same format as webhook returns)."""
        method = response_data.get("method")
        if not method:
            return
        assert self._http is not None
        try:
            payload = {k: v for k, v in response_data.items() if k != "method"}
            self._http.post(
                f"{self._api_base}/{method}",
                json=payload,
                timeout=10,
            )
        except Exception:
            logger.exception("Failed to execute Telegram method %s", method)

    # ------------------------------------------------------------------
    # Update processing
    # ------------------------------------------------------------------

    def _process_update(self, update: dict) -> None:
        """Process a single Telegram update."""
        update_id = update.get("update_id", 0)
        self.offset = update_id + 1

        # Set service-role RLS context for DB access
        from apps.tenants.middleware import set_rls_context
        set_rls_context(service_role=True)

        try:
            self._handle_update(update)
        except Exception:
            logger.exception("Unhandled error processing update %s", update_id)

    def _handle_update(self, update: dict) -> None:
        """Core routing logic for a single update."""
        # Handle /start TOKEN for account linking
        link_response = handle_start_command(update)
        if link_response:
            self._execute_telegram_response(link_response)
            return

        chat_id = extract_chat_id(update)
        if not chat_id:
            return

        # Rate limiting
        if is_rate_limited(chat_id):
            logger.warning("Rate limited chat_id %s", chat_id)
            return

        tenant = resolve_tenant_by_chat_id(chat_id)

        # Handle lesson approval callbacks
        if "callback_query" in update and tenant is not None:
            callback_data = update["callback_query"].get("data", "")
            if callback_data.startswith("lesson:"):
                self._handle_lesson_callback(update, tenant)
                return

        # Unknown user → onboarding
        if not tenant:
            response_data = send_onboarding_link(chat_id)
            self._execute_telegram_response(response_data)
            logger.info("Unknown chat_id %s, sent onboarding link", chat_id)
            return

        # Provisioning tenant — assistant is still waking up
        if tenant.status in (Tenant.Status.PENDING, Tenant.Status.PROVISIONING):
            self._send_message(
                chat_id,
                "Your assistant is waking up! 🌅 This usually takes about a minute. "
                "I'll be ready to chat shortly — just send your message again in a moment!",
            )
            return

        # Suspended tenant without subscription
        frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
        if (
            tenant.status == Tenant.Status.SUSPENDED
            and not tenant.is_trial
            and not bool(tenant.stripe_subscription_id)
        ):
            self._send_message(
                chat_id,
                f"Your free trial has ended. Subscribe to continue using your assistant: "
                f"{frontend_url}/settings/billing",
            )
            return

        # Budget check
        if not check_budget(tenant):
            self._send_budget_exhausted(chat_id, tenant)
            return

        # Update last_message_at
        Tenant.objects.filter(id=tenant.id).update(last_message_at=timezone.now())

        # Extract message text
        message_text = self._extract_message_text(update)
        if not message_text:
            return

        # Forward to container via /v1/chat/completions
        self._forward_to_container(chat_id, tenant, message_text)

    def _extract_message_text(self, update: dict) -> str | None:
        """Extract user message text from a Telegram update."""
        message = update.get("message") or update.get("edited_message")
        if not message:
            return None

        text = message.get("text")
        if text:
            return text

        # Photo with caption
        caption = message.get("caption")
        if caption:
            return caption

        # Photo without caption
        if message.get("photo"):
            return "[User sent a photo]"

        # Voice/audio
        if message.get("voice") or message.get("audio"):
            return "[User sent a voice message]"

        # Document
        if message.get("document"):
            return "[User sent a document]"

        # Sticker
        if message.get("sticker"):
            emoji = message["sticker"].get("emoji", "")
            return f"[User sent a sticker {emoji}]"

        # Video
        if message.get("video") or message.get("video_note"):
            return "[User sent a video]"

        # Location
        if message.get("location"):
            return "[User shared a location]"

        # Contact
        if message.get("contact"):
            return "[User shared a contact]"

        return None

    def _forward_to_container(self, chat_id: int, tenant: Tenant, message_text: str) -> None:
        """Send the message to the tenant's OpenClaw container and relay the response."""
        if not tenant.container_fqdn:
            self._send_message(chat_id, "Your assistant is being set up. Please try again in a minute!")
            return

        url = f"https://{tenant.container_fqdn}/v1/chat/completions"
        user_tz = tenant.user.timezone or "UTC"

        try:
            resp = httpx.post(
                url,
                json={
                    "model": "openclaw",
                    "messages": [{"role": "user", "content": message_text}],
                },
                headers={
                    "Authorization": f"Bearer {self.webhook_secret}",
                    "X-Telegram-Bot-Api-Secret-Token": self.webhook_secret,
                    "X-User-Timezone": user_tz,
                    "X-Telegram-Chat-Id": str(chat_id),
                },
                timeout=CHAT_COMPLETIONS_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json()
        except httpx.TimeoutException:
            logger.warning("Timeout forwarding to %s for chat_id=%s", tenant.container_fqdn, chat_id)
            # Container likely received it and will respond async — don't send error
            return
        except httpx.HTTPError as e:
            logger.error("Error forwarding to %s: %s", tenant.container_fqdn, e)
            self._send_message(chat_id, "Sorry, I'm having trouble connecting right now. Please try again shortly.")
            return

        # Extract AI response
        ai_text = self._extract_ai_response(result)
        if ai_text:
            self._send_message(chat_id, ai_text)

        # Record usage
        self._record_usage(tenant, result)

    def _extract_ai_response(self, result: dict) -> str | None:
        """Extract the AI response text from a chat completions response."""
        try:
            choices = result.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content")
        except (IndexError, KeyError, TypeError):
            pass

        # Fallback: check if it's the old webhook format with a direct text response
        if "text" in result:
            return result["text"]

        return None

    def _record_usage(self, tenant: Tenant, result: dict) -> None:
        """Record token usage from the chat completions response."""
        usage = result.get("usage", {})
        if not isinstance(usage, dict):
            return

        input_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
        output_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
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
                logger.exception("Failed to record usage for tenant %s", tenant.id)

    def _send_budget_exhausted(self, chat_id: int, tenant: Tenant) -> None:
        """Send budget exhausted message."""
        frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
        budget_remaining = max(tenant.monthly_token_budget - tenant.tokens_this_month, 0)
        plus_message = (
            " Opus requests are paused while at quota."
            if tenant.model_tier == Tenant.ModelTier.PREMIUM
            else ""
        )
        self._send_message(
            chat_id,
            f"You've hit your monthly token quota."
            f" {budget_remaining} token{'s' if budget_remaining != 1 else ''} remaining."
            f" New messages are blocked until the next monthly reset."
            f"{plus_message} Open Billing to upgrade/manage at {frontend_url}/billing.",
        )

    def _handle_lesson_callback(self, update: dict, tenant: Tenant) -> None:
        """Handle lesson approval callback queries via the existing handler.

        The existing handle_lesson_callback returns a JsonResponse; we extract
        the JSON content and execute the Telegram method it describes.
        """
        from .lesson_callbacks import handle_lesson_callback

        try:
            json_response = handle_lesson_callback(update, tenant)
            # JsonResponse stores rendered content; parse it back
            import json as _json
            response_data = _json.loads(json_response.content)
            if response_data:
                self._execute_telegram_response(response_data)
        except Exception:
            logger.exception("Error handling lesson callback")
            callback_id = update["callback_query"].get("id")
            if callback_id:
                self._answer_callback_query(callback_id, "Something went wrong")
