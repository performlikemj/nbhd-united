"""Central Telegram poller — one process polls getUpdates for the shared bot,
routes each message to the correct tenant's OpenClaw container via the
/telegram-webhook endpoint, then sends the AI reply back to the user."""

from __future__ import annotations

import logging
import os
import signal
import time
import threading
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
        self.gateway_token: str = getattr(settings, "NBHD_INTERNAL_API_KEY", "").strip()
        self.offset: int = 0
        self._running = False
        self._backoff = 1
        self._http: httpx.Client | None = None
        self._pending_messages: dict[int, str] = {}  # chat_id → message awaiting container update

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

    def _send_markdown(self, chat_id: int, text: str) -> None:
        """Send a message with Markdown formatting, falling back to plain text.

        Tries Markdown parse_mode first (supports **bold**, _italic_, `code`).
        If Telegram rejects it (malformed markdown), retries as plain text.
        """
        assert self._http is not None
        # Try Markdown (legacy mode — more forgiving than MarkdownV2)
        try:
            resp = self._http.post(
                f"{self._api_base}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            if resp.is_success:
                return
            # If Telegram rejected the markdown (400), fall back to plain text
            if resp.status_code == 400:
                logger.debug("Markdown rejected, falling back to plain text")
                self._send_message(chat_id, text)
                return
            logger.warning("sendMessage(Markdown) failed (%s): %s", resp.status_code, resp.text[:200])
        except Exception:
            # Network error — try plain text
            self._send_message(chat_id, text)

    def _send_typing(self, chat_id: int) -> None:
        """Send 'typing' chat action to Telegram."""
        assert self._http is not None
        try:
            self._http.post(
                f"{self._api_base}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
                timeout=5,
            )
        except Exception:
            pass  # Non-critical, don't log

    def _transcribe_voice(self, file_id: str) -> str | None:
        """Download a Telegram voice file and transcribe via OpenAI Whisper.

        Returns transcribed text, or None on failure.
        """
        assert self._http is not None
        openai_key = getattr(settings, "OPENAI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        if not openai_key:
            logger.warning("Cannot transcribe voice: no OPENAI_API_KEY configured")
            return None

        try:
            # 1. Get file path from Telegram
            resp = self._http.post(
                f"{self._api_base}/getFile",
                json={"file_id": file_id},
                timeout=10,
            )
            if not resp.is_success:
                logger.warning("getFile failed: %s", resp.text[:200])
                return None

            file_path = resp.json().get("result", {}).get("file_path")
            if not file_path:
                logger.warning("getFile returned no file_path")
                return None

            # 2. Download the audio file
            file_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
            dl_resp = self._http.get(file_url, timeout=15)
            if not dl_resp.is_success:
                logger.warning("Failed to download voice file: %s", dl_resp.status_code)
                return None

            audio_data = dl_resp.content
            # Determine extension from file_path
            ext = file_path.rsplit(".", 1)[-1] if "." in file_path else "ogg"

            # 3. Transcribe via OpenAI Whisper API
            whisper_resp = self._http.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {openai_key}"},
                files={"file": (f"voice.{ext}", audio_data, f"audio/{ext}")},
                data={"model": "whisper-1"},
                timeout=30,
            )
            if not whisper_resp.is_success:
                logger.warning("Whisper transcription failed: %s %s", whisper_resp.status_code, whisper_resp.text[:200])
                return None

            text = whisper_resp.json().get("text", "").strip()
            if text:
                logger.info("Transcribed voice message (%d bytes → %d chars)", len(audio_data), len(text))
                return text

            return None

        except Exception:
            logger.exception("Voice transcription error")
            return None

    def _delayed_forward(self, chat_id: int, tenant: Tenant, message_text: str, delay: int = 15) -> None:
        """Wait for container restart, then forward the pending message.

        Runs in a background thread to avoid blocking the poller loop.
        """
        time.sleep(delay)
        self._send_typing(chat_id)
        self._forward_to_container(chat_id, tenant, message_text)

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

        # Handle callback queries (button presses)
        if "callback_query" in update and tenant is not None:
            callback_data = update["callback_query"].get("data", "")
            callback_id = update["callback_query"].get("id", "")

            # Onboarding callbacks (tz_country:, tz_zone:)
            if callback_data.startswith("tz_"):
                from apps.router.onboarding import handle_onboarding_callback
                reply = handle_onboarding_callback(tenant, callback_data)
                if reply is not None:
                    self._answer_callback_query(callback_id, "✓")
                    self._send_message(chat_id, reply.text, **reply.to_telegram_kwargs())
                return

            # Lesson approval callbacks
            if callback_data.startswith("lesson:"):
                self._handle_lesson_callback(update, tenant)
                return

            # Container update callbacks
            if callback_data.startswith("container_update:"):
                from apps.router.container_updates import handle_update_callback
                reply_text = handle_update_callback(tenant, callback_data)
                if reply_text:
                    self._answer_callback_query(callback_id, "✓")
                    self._send_message(chat_id, reply_text)

                # If they said yes and we have a pending message, forward after restart
                if callback_data == "container_update:yes" and chat_id in self._pending_messages:
                    pending_text = self._pending_messages.pop(chat_id)
                    threading.Thread(
                        target=self._delayed_forward,
                        args=(chat_id, tenant, pending_text, 15),
                        daemon=True,
                    ).start()
                elif callback_data == "container_update:no" and chat_id in self._pending_messages:
                    # They said later — forward the message to current (old) container
                    pending_text = self._pending_messages.pop(chat_id)
                    self._send_typing(chat_id)
                    self._forward_to_container(chat_id, tenant, pending_text)
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

        # Send typing indicator for voice messages (transcription takes a few seconds)
        msg_obj = update.get("message") or update.get("edited_message") or {}
        if msg_obj.get("voice") or msg_obj.get("audio"):
            self._send_typing(chat_id)

        # Extract message text
        message_text = self._extract_message_text(update)
        if not message_text:
            return

        # Onboarding / re-introduction gate
        from apps.router.onboarding import get_onboarding_response, needs_reintroduction

        if needs_reintroduction(tenant):
            # Existing user with default profile — trigger re-intro
            tenant.onboarding_step = 0
            # onboarding_complete stays True so get_onboarding_response
            # knows to use the re-intro message (not fresh welcome)
            tenant.save(update_fields=["onboarding_step", "updated_at"])

        if not tenant.onboarding_complete or tenant.onboarding_step == 0:
            # Extract Telegram language_code for auto-detection
            msg = update.get("message") or update.get("edited_message") or {}
            tg_lang = (msg.get("from") or {}).get("language_code", "")
            onboarding_reply = get_onboarding_response(tenant, message_text, telegram_lang=tg_lang)
            if onboarding_reply is not None:
                self._send_message(chat_id, onboarding_reply.text, **onboarding_reply.to_telegram_kwargs())
                return

        # Check for container updates before forwarding
        from apps.router.container_updates import check_and_maybe_update
        update_action = check_and_maybe_update(tenant)
        if update_action:
            if update_action["action"] == "ask_user":
                # Store the pending message so we can forward it after update
                self._pending_messages[chat_id] = message_text
                self._send_message(
                    chat_id,
                    update_action["text"],
                    reply_markup=update_action["reply_markup"],
                )
                return
            # "silent_update" — container is restarting, delay then forward in background
            if update_action["action"] == "silent_update":
                self._send_typing(chat_id)
                threading.Thread(
                    target=self._delayed_forward,
                    args=(chat_id, tenant, message_text, 15),
                    daemon=True,
                ).start()
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

        # Voice/audio — transcribe via Whisper
        voice = message.get("voice") or message.get("audio")
        if voice:
            file_id = voice.get("file_id")
            if file_id:
                transcript = self._transcribe_voice(file_id)
                if transcript:
                    return f"🎤 Voice message: \"{transcript}\""
            return "[User sent a voice message — couldn't transcribe, please try sending as text]"

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
        """Send the message to the tenant's OpenClaw container via /v1/chat/completions and relay the response."""
        if not tenant.container_fqdn:
            self._send_message(chat_id, "Your assistant is being set up. Please try again in a minute!")
            return

        url = f"https://{tenant.container_fqdn}/v1/chat/completions"
        user_tz = tenant.user.timezone or "UTC"

        # Show typing indicator while waiting for AI response
        self._send_typing(chat_id)
        typing_stop = threading.Event()

        def _keep_typing():
            while not typing_stop.wait(5.0):
                self._send_typing(chat_id)

        typing_thread = threading.Thread(target=_keep_typing, daemon=True)
        typing_thread.start()

        try:
            resp = httpx.post(
                url,
                json={
                    "model": "openclaw",
                    "messages": [{"role": "user", "content": message_text}],
                    "user": str(chat_id),
                },
                headers={
                    "Authorization": f"Bearer {self.gateway_token}",
                    "X-User-Timezone": user_tz,
                    "X-Telegram-Chat-Id": str(chat_id),
                },
                timeout=CHAT_COMPLETIONS_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json()
        except httpx.TimeoutException:
            logger.warning("Timeout forwarding to %s for chat_id=%s", tenant.container_fqdn, chat_id)
            return
        except httpx.HTTPStatusError as e:
            logger.error("FWD_FAIL %s HTTP %s", tenant.container_fqdn, e.response.status_code)
            logger.error("FWD_BODY %s", (e.response.text[:300] if e.response else "none"))
            self._send_message(chat_id, "Sorry, I'm having trouble connecting right now. Please try again shortly.")
            return
        except httpx.HTTPError as e:
            logger.error("Error forwarding to %s: %s", tenant.container_fqdn, e)
            self._send_message(chat_id, "Sorry, I'm having trouble connecting right now. Please try again shortly.")
            return
        finally:
            typing_stop.set()

        # Extract AI response text from OpenAI-compatible response
        ai_text = self._extract_ai_response(result)
        if ai_text:
            self._send_markdown(chat_id, ai_text)

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
        return None

    def _record_usage(self, tenant: Tenant, result: dict) -> None:
        """Record token usage from the webhook response."""
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
