"""Central Telegram poller — one process polls getUpdates for the shared bot,
routes each message to the correct tenant's OpenClaw container via the
/telegram-webhook endpoint, then sends the AI reply back to the user."""

from __future__ import annotations

import base64
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
        self._update_in_progress: set[int] = set()  # chat_ids currently being updated

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

    @staticmethod
    def _split_message(text: str, max_len: int = 4096) -> list[str]:
        """Split a long message into chunks that fit Telegram's limit.

        Splits on paragraph breaks first, then newlines, then hard cuts.
        """
        if len(text) <= max_len:
            return [text]

        chunks: list[str] = []
        remaining = text

        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break

            # Try to split at paragraph break
            cut = remaining.rfind("\n\n", 0, max_len)
            if cut == -1:
                # Try newline
                cut = remaining.rfind("\n", 0, max_len)
            if cut == -1:
                # Try space
                cut = remaining.rfind(" ", 0, max_len)
            if cut == -1:
                # Hard cut
                cut = max_len

            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()

        return [c for c in chunks if c]  # Drop empty chunks

    def _send_markdown(self, chat_id: int, text: str) -> None:
        """Send a message with Markdown formatting, falling back to plain text.

        Handles long messages by splitting into chunks. Tries Markdown
        parse_mode first; if Telegram rejects it, retries as plain text.
        """
        assert self._http is not None
        chunks = self._split_message(text)

        for i, chunk in enumerate(chunks):
            if i > 0:
                time.sleep(0.3)  # Brief delay between chunks

            try:
                resp = self._http.post(
                    f"{self._api_base}/sendMessage",
                    json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
                    timeout=10,
                )
                if resp.is_success:
                    continue
                # If Telegram rejected the markdown (400), fall back to plain text
                if resp.status_code == 400:
                    logger.debug("Markdown rejected for chunk %d, falling back to plain text", i)
                    self._send_message(chat_id, chunk)
                    continue
                logger.warning("sendMessage(Markdown) failed (%s): %s", resp.status_code, resp.text[:200])
            except Exception:
                # Network error — try plain text
                self._send_message(chat_id, chunk)

    def _send_photo(self, chat_id: int, photo_path: str, tenant: Tenant, caption: str = "") -> bool:
        """Send a photo to Telegram from the tenant's workspace file share.

        Returns True if sent successfully.
        """
        assert self._http is not None
        try:
            # Map container path back to file share path
            # Container: /home/node/.openclaw/workspace/X → File share: workspace/X
            share_path = photo_path
            if "/workspace/" in share_path:
                share_path = "workspace/" + share_path.split("/workspace/", 1)[1]

            # Download from file share
            from apps.orchestrator.azure_client import _is_mock
            if _is_mock():
                return False

            account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
            if not account_name:
                return False

            from azure.storage.fileshare import ShareFileClient
            from apps.orchestrator.azure_client import get_storage_client

            storage_client = get_storage_client()
            keys = storage_client.storage_accounts.list_keys(
                settings.AZURE_RESOURCE_GROUP, account_name,
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

            # Send via Telegram sendPhoto
            ext = share_path.rsplit(".", 1)[-1].lower() if "." in share_path else "jpg"
            mime = {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
            files = {"photo": (f"image.{ext}", data, mime)}
            form_data = {"chat_id": str(chat_id)}
            if caption:
                form_data["caption"] = caption[:1024]  # Telegram caption limit
                form_data["parse_mode"] = "Markdown"

            resp = self._http.post(
                f"{self._api_base}/sendPhoto",
                data=form_data,
                files=files,
                timeout=15,
            )
            if resp.is_success:
                return True
            logger.warning("sendPhoto failed (%s): %s", resp.status_code, resp.text[:200])
            return False

        except Exception:
            logger.exception("Failed to send photo from %s", photo_path)
            return False

    def _send_rich_response(self, chat_id: int, tenant: Tenant, text: str) -> None:
        """Send an AI response, extracting and sending any embedded images.

        Detects image references like MEDIA:/path/to/image.jpg or
        workspace file paths, sends them as Telegram photos, and sends
        the remaining text as markdown.
        """
        import re

        # Pattern: MEDIA:path or common image extensions in workspace paths
        # OpenClaw uses MEDIA:./path or MEDIA:https://... convention
        media_pattern = re.compile(
            r'MEDIA:(\S+\.(?:jpg|jpeg|png|gif|webp))',
            re.IGNORECASE,
        )

        # Also detect workspace image paths the agent might reference
        workspace_pattern = re.compile(
            r'(/home/node/\.openclaw/workspace/\S+\.(?:jpg|jpeg|png|gif|webp))',
            re.IGNORECASE,
        )

        # Extract all image references
        media_matches = media_pattern.findall(text)
        workspace_matches = workspace_pattern.findall(text)

        # Clean image references from text
        clean_text = media_pattern.sub('', text)
        clean_text = workspace_pattern.sub('', clean_text)
        clean_text = clean_text.strip()

        # Send images first
        for path in media_matches + workspace_matches:
            # Resolve relative paths
            if path.startswith("./"):
                path = f"/home/node/.openclaw/workspace/{path[2:]}"
            if path.startswith("/home/node/"):
                self._send_photo(chat_id, path, tenant)

        # Parse inline buttons: [[button:Label|callback_data]]
        button_pattern = re.compile(r'\[\[button:([^|]+)\|([^\]]+)\]\]')
        buttons = button_pattern.findall(clean_text)
        clean_text = button_pattern.sub('', clean_text).strip()

        # Build reply_markup if buttons found
        reply_markup = None
        if buttons:
            keyboard = [[{"text": label.strip(), "callback_data": f"agent:{data.strip()}"}] for label, data in buttons]
            reply_markup = {"inline_keyboard": keyboard}

        # Send remaining text
        if clean_text:
            if reply_markup:
                # Send last chunk with buttons attached
                chunks = self._split_message(clean_text)
                for i, chunk in enumerate(chunks):
                    if i > 0:
                        time.sleep(0.3)
                    if i == len(chunks) - 1:
                        # Last chunk gets the buttons
                        self._send_message(chat_id, chunk, parse_mode="Markdown", reply_markup=reply_markup)
                    else:
                        self._send_markdown(chat_id, chunk)
            else:
                self._send_markdown(chat_id, clean_text)

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

    def _download_photo(self, message: dict) -> str | None:
        """Download the largest photo from a Telegram message and return as base64 data URL.

        Returns None if download fails or photo is too large (>5MB).
        """
        photos = message.get("photo", [])
        if not photos:
            return None

        # Telegram provides multiple sizes, last is largest
        largest = photos[-1]
        file_id = largest.get("file_id")
        file_size = largest.get("file_size", 0)

        if not file_id:
            return None
        if file_size > 5 * 1024 * 1024:  # 5MB limit
            logger.warning("Photo too large (%d bytes), skipping", file_size)
            return None

        assert self._http is not None
        try:
            # Get file path
            resp = self._http.post(
                f"{self._api_base}/getFile",
                json={"file_id": file_id},
                timeout=10,
            )
            if not resp.is_success:
                return None

            file_path = resp.json().get("result", {}).get("file_path")
            if not file_path:
                return None

            # Download
            file_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
            dl_resp = self._http.get(file_url, timeout=15)
            if not dl_resp.is_success:
                return None

            b64 = base64.b64encode(dl_resp.content).decode()
            ext = file_path.rsplit(".", 1)[-1] if "." in file_path else "jpg"
            return f"data:image/{ext};base64,{b64}"

        except Exception:
            logger.exception("Failed to download photo")
            return None

    def _upload_photo_to_workspace(self, tenant: Tenant, message: dict) -> str | None:
        """Download photo from Telegram and upload to tenant's workspace.

        Returns the workspace-relative file path, or None on failure.
        """
        photo_data_url = self._download_photo(message)
        if not photo_data_url:
            return None

        try:
            # Extract binary from data URL
            # Format: data:image/jpg;base64,<data>
            header, b64data = photo_data_url.split(",", 1)
            ext = "jpg"
            if "png" in header:
                ext = "png"
            elif "gif" in header:
                ext = "gif"
            elif "webp" in header:
                ext = "webp"

            photo_bytes = base64.b64decode(b64data)

            # Generate unique filename
            import hashlib
            name_hash = hashlib.sha256(photo_bytes[:1024]).hexdigest()[:8]
            filename = f"photo_{name_hash}.{ext}"
            workspace_path = f"workspace/media/inbound/{filename}"
            local_path = f"/home/node/.openclaw/workspace/media/inbound/{filename}"

            # Upload to tenant's file share
            from apps.orchestrator.azure_client import upload_workspace_file_binary
            tenant_id = str(tenant.id)
            upload_workspace_file_binary(tenant_id, workspace_path, photo_bytes)
            logger.info("Uploaded photo to %s for tenant %s", workspace_path, tenant_id[:8])
            return local_path

        except Exception:
            logger.exception("Failed to upload photo to workspace")
            return None

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

    def _edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict | None) -> None:
        """Edit a message's inline keyboard (or remove it)."""
        assert self._http is not None
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        else:
            payload["reply_markup"] = {"inline_keyboard": []}
        try:
            self._http.post(
                f"{self._api_base}/editMessageReplyMarkup",
                json=payload,
                timeout=5,
            )
        except Exception:
            pass  # Non-critical

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

            # Proactive extraction callbacks (goals/tasks/lessons from nightly job)
            if callback_data.startswith("extract:"):
                self._handle_extraction_callback(update, tenant)
                return

            # Action gate callbacks (approve/deny destructive actions)
            if callback_data.startswith("gate_approve:") or callback_data.startswith("gate_deny:"):
                self._handle_gate_callback(update, tenant, callback_data, callback_id, chat_id)
                return

            # Container update callbacks
            if callback_data.startswith("container_update:"):
                # Debounce: check if already processing
                if chat_id in self._update_in_progress:
                    self._answer_callback_query(callback_id, "⏳ Already updating...")
                    return
                
                from apps.router.container_updates import handle_update_callback

                # Edit the original message to remove buttons (prevents re-tapping)
                orig_msg_id = update["callback_query"].get("message", {}).get("message_id")
                if orig_msg_id:
                    self._edit_message_reply_markup(chat_id, orig_msg_id, None)

                if callback_data == "container_update:yes":
                    self._update_in_progress.add(chat_id)
                    self._answer_callback_query(callback_id, "✅ Updating now...")
                    self._send_message(chat_id, "✅ Updating now! I'll be back in about a minute...")

                    # Do the actual update + forward in background (Azure takes 45-90s)
                    pending_text = self._pending_messages.pop(chat_id, None)

                    def _do_update():
                        try:
                            from apps.router.container_updates import update_container
                            success = update_container(tenant)
                            if not success:
                                self._send_message(chat_id, "Sorry, the update failed. Your assistant is still available on the current version.")
                                return
                            if pending_text:
                                # Wait for container gateway to be healthy before forwarding
                                if self._wait_for_container_ready(tenant, timeout=120):
                                    # Prepend context note so agent isn't cold
                                    context_msg = (
                                        f"[System: just updated. User's message from before the update:]\n{pending_text}"
                                    )
                                    self._send_typing(chat_id)
                                    self._forward_to_container(chat_id, tenant, context_msg)
                                else:
                                    # Container didn't come up in time — tell user, let them resend
                                    self._send_message(chat_id, "✅ Update complete! You can send your message again.")
                        finally:
                            self._update_in_progress.discard(chat_id)

                    threading.Thread(target=_do_update, daemon=True).start()

                elif callback_data == "container_update:no":
                    self._answer_callback_query(callback_id, "👍")
                    reply_text = handle_update_callback(tenant, callback_data)
                    if reply_text:
                        self._send_message(chat_id, reply_text)
                    # Forward pending message to current container
                    pending_text = self._pending_messages.pop(chat_id, None)
                    if pending_text:
                        self._send_typing(chat_id)
                        self._forward_to_container(chat_id, tenant, pending_text)

                return

            # Agent-generated button callbacks → forward to agent as a message
            if callback_data.startswith("agent:"):
                button_value = callback_data[6:]  # Strip "agent:" prefix
                self._answer_callback_query(callback_id, "✓")
                # Remove buttons from the original message
                orig_msg_id = update["callback_query"].get("message", {}).get("message_id")
                if orig_msg_id:
                    self._edit_message_reply_markup(chat_id, orig_msg_id, None)
                # Forward the button tap to the agent
                self._send_typing(chat_id)
                self._forward_to_container(chat_id, tenant, f'[User tapped button: "{button_value}"]')
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

        # Paused tenant — trial ended or payment lapsed
        frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
        if (
            tenant.status == Tenant.Status.SUSPENDED
            and not tenant.is_trial
            and not bool(tenant.stripe_subscription_id)
        ):
            self._send_message(
                chat_id,
                "Your assistant is paused. Running an AI agent costs real money "
                "— cloud servers, model tokens (every reply costs us), and storage. "
                "We keep things transparent so you know exactly where your money goes.\n\n"
                f"Ready to pick up where you left off? {frontend_url}/settings/billing",
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

        # Upload photo to tenant workspace if present
        image_path = None
        msg_data = update.get("message") or update.get("edited_message") or {}
        if msg_data.get("photo"):
            self._send_typing(chat_id)
            image_path = self._upload_photo_to_workspace(tenant, msg_data)

        # Forward to container via /v1/chat/completions
        if image_path:
            # Tell agent where the image is so it can use the image tool
            message_text = f"[Photo attached: {image_path}]\n{message_text}"

        # Contextual recall: inject goals/tasks + relevant history on session start
        if self._is_new_session(tenant):
            message_text = self._build_session_context(tenant, message_text)

        self._forward_to_container(chat_id, tenant, message_text)

    # ── Contextual recall ──────────────────────────────────────────────────

    SESSION_GAP_SECONDS = 30 * 60  # 30 minutes

    def _is_new_session(self, tenant: Tenant) -> bool:
        """Return True if enough time has passed since the tenant's last message."""
        try:
            if not tenant.last_message_at:
                return True  # First ever message
            elapsed = (timezone.now() - tenant.last_message_at).total_seconds()
            return elapsed > self.SESSION_GAP_SECONDS
        except (TypeError, AttributeError):
            return False

    def _build_session_context(self, tenant: Tenant, message_text: str) -> str:
        """Prepend goals, tasks, and relevant history to the message.

        Called on the first message after a 30-min gap. Best-effort — if anything
        fails (embedding API down, no docs), returns the original message unchanged.
        """
        try:
            return self._build_session_context_inner(tenant, message_text)
        except Exception:
            logger.exception("contextual_recall: failed for tenant %s", str(tenant.id)[:8])
            return message_text

    def _build_session_context_inner(self, tenant: Tenant, message_text: str) -> str:
        from apps.journal.models import Document, DocumentChunk
        from apps.lessons.models import Lesson

        context_parts: list[str] = []

        # Step 1: Always inject goals + tasks
        goals_doc = Document.objects.filter(
            tenant=tenant, kind=Document.Kind.GOAL, slug="goals"
        ).first()
        if goals_doc and goals_doc.markdown.strip():
            context_parts.append(f"## Your active goals\n{goals_doc.markdown[:1500]}")

        tasks_doc = Document.objects.filter(
            tenant=tenant, kind=Document.Kind.TASKS, slug="tasks"
        ).first()
        if tasks_doc and tasks_doc.markdown.strip():
            context_parts.append(f"## Your current tasks\n{tasks_doc.markdown[:1500]}")

        # Step 2: Search relevant history (best-effort — skip if embedding fails)
        history_parts: list[str] = []
        try:
            from apps.lessons.services import generate_embedding
            from pgvector.django import CosineDistance

            query_embedding = generate_embedding(message_text[:500])

            # Search daily note chunks
            chunks = (
                DocumentChunk.objects
                .filter(tenant=tenant)
                .annotate(distance=CosineDistance("embedding", query_embedding))
                .order_by("distance")[:5]
            )
            for c in chunks:
                similarity = 1.0 - float(c.distance)
                if similarity > 0.65:
                    history_parts.append(c.text)
                if len(history_parts) >= 2:
                    break

            # Search lessons
            lessons = (
                Lesson.objects
                .filter(tenant=tenant, status="approved", embedding__isnull=False)
                .annotate(distance=CosineDistance("embedding", query_embedding))
                .order_by("distance")[:3]
            )
            for lsn in lessons:
                similarity = 1.0 - float(lsn.distance)
                if similarity > 0.65 and len(history_parts) < 3:
                    history_parts.append(f"💡 {lsn.text}")

        except Exception:
            logger.warning("contextual_recall: embedding search failed for tenant %s", str(tenant.id)[:8], exc_info=True)

        if history_parts:
            context_parts.append("## Relevant history\n" + "\n---\n".join(history_parts))

        if not context_parts:
            return message_text

        prefix = "[Context for this conversation:]\n\n" + "\n\n".join(context_parts)
        prefix += "\n\n[Now respond to the user's message:]\n\n"
        return prefix + message_text

    def _extract_reply_context(self, message: dict) -> str:
        """Extract reply-to context if user is replying to a bot message.

        Returns a prefix string like '[Replying to: "truncated text"]\n\n' or empty string.
        """
        reply = message.get("reply_to_message")
        if not reply:
            return ""

        # Only include context for replies to bot messages
        reply_from = reply.get("from", {})
        if not reply_from.get("is_bot"):
            return ""

        reply_text = reply.get("text") or reply.get("caption") or ""
        if not reply_text:
            return ""

        # Truncate long quotes
        if len(reply_text) > 200:
            reply_text = reply_text[:200] + "…"

        return f'[Replying to: "{reply_text}"]\n\n'

    def _extract_message_text(self, update: dict) -> str | None:
        """Extract user message text from a Telegram update."""
        message = update.get("message") or update.get("edited_message")
        if not message:
            return None

        reply_prefix = self._extract_reply_context(message)

        # Forwarded message — prepend source info
        if message.get("forward_from") or message.get("forward_from_chat"):
            fwd_name = ""
            if message.get("forward_from"):
                fwd_name = message["forward_from"].get("first_name", "someone")
            elif message.get("forward_from_chat"):
                fwd_name = message["forward_from_chat"].get("title", "a chat")
            fwd_text = message.get("text") or message.get("caption") or ""
            return f"{reply_prefix}[Forwarded from {fwd_name}]\n{fwd_text}"

        text = message.get("text")
        if text:
            return f"{reply_prefix}{text}"

        # Photo — handled separately via _upload_photo_to_workspace
        if message.get("photo"):
            caption = message.get("caption") or "User sent a photo"
            return f"{reply_prefix}{caption}"

        # Voice/audio — transcribe via Whisper
        voice = message.get("voice") or message.get("audio")
        if voice:
            file_id = voice.get("file_id")
            if file_id:
                transcript = self._transcribe_voice(file_id)
                if transcript:
                    return f'{reply_prefix}🎤 Voice message: "{transcript}"'
            return f"{reply_prefix}[Voice message — couldn't transcribe, please try sending as text]"

        # Document — download and extract text for supported types
        doc = message.get("document")
        if doc:
            return f"{reply_prefix}{self._extract_document_text(doc)}"

        # Sticker
        if message.get("sticker"):
            emoji = message["sticker"].get("emoji", "")
            return f"{reply_prefix}[User sent a sticker {emoji}]"

        # Video — include metadata
        video = message.get("video") or message.get("video_note")
        if video:
            duration = video.get("duration", 0)
            file_size = video.get("file_size", 0)
            size_mb = f"{file_size / (1024 * 1024):.1f}" if file_size else "?"
            caption = message.get("caption") or ""
            meta = f"[User sent a video ({duration}s, {size_mb} MB)]"
            if caption:
                meta += f"\nCaption: {caption}"
            return f"{reply_prefix}{meta}"

        # Location
        loc = message.get("location")
        if loc:
            lat = loc.get("latitude", 0)
            lng = loc.get("longitude", 0)
            venue = message.get("venue")
            if venue:
                name = venue.get("title", "")
                addr = venue.get("address", "")
                return f"{reply_prefix}📍 User shared a venue: {name} — {addr} ({lat}, {lng}) https://maps.google.com/maps?q={lat},{lng}"
            return f"{reply_prefix}📍 User shared their location: {lat}, {lng} https://maps.google.com/maps?q={lat},{lng}"

        # Contact
        contact = message.get("contact")
        if contact:
            name = f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip()
            phone = contact.get("phone_number", "")
            return f"{reply_prefix}📇 User shared a contact: {name} ({phone})"

        return None

    def _extract_document_text(self, doc: dict) -> str:
        """Extract document info. Downloads text-based files for content extraction."""
        file_name = doc.get("file_name", "unknown")
        mime_type = doc.get("mime_type", "")
        file_size = doc.get("file_size", 0)
        file_id = doc.get("file_id")

        # Size limit: 10MB
        if file_size > 10 * 1024 * 1024:
            return f"[User sent a document: {file_name} ({file_size / (1024*1024):.1f} MB) — too large to process]"

        # Text-based files we can read
        text_extensions = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".py", ".js", ".ts", ".yaml", ".yml", ".toml", ".log"}
        text_mimes = {"text/", "application/json", "application/xml", "application/yaml"}

        ext = ""
        if "." in file_name:
            ext = "." + file_name.rsplit(".", 1)[-1].lower()

        is_text = ext in text_extensions or any(mime_type.startswith(m) for m in text_mimes)

        if not is_text or not file_id:
            return f"[User sent a document: {file_name} ({mime_type})]"

        # Download and read content
        assert self._http is not None
        try:
            resp = self._http.post(
                f"{self._api_base}/getFile",
                json={"file_id": file_id},
                timeout=10,
            )
            if not resp.is_success:
                return f"[User sent a document: {file_name} — download failed]"

            file_path = resp.json().get("result", {}).get("file_path")
            if not file_path:
                return f"[User sent a document: {file_name} — download failed]"

            file_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
            dl_resp = self._http.get(file_url, timeout=15)
            if not dl_resp.is_success:
                return f"[User sent a document: {file_name} — download failed]"

            content = dl_resp.content.decode("utf-8", errors="replace")
            # Truncate very long files
            if len(content) > 10000:
                content = content[:10000] + "\n\n[... truncated, file continues ...]"

            return f"📄 Document: {file_name}\n```\n{content}\n```"

        except Exception:
            logger.exception("Failed to download document %s", file_name)
            return f"[User sent a document: {file_name} — download failed]"

    def _wait_for_container_ready(self, tenant: Tenant, timeout: int = 120) -> bool:
        """Poll the container's health endpoint until it's up or timeout expires.

        Returns True if the container came up within the timeout, False otherwise.
        """
        import time as _time
        deadline = _time.time() + timeout
        fqdn = tenant.container_fqdn
        if not fqdn:
            return False
        url = f"https://{fqdn}/health"
        internal_key = getattr(settings, "NBHD_INTERNAL_API_KEY", "")
        while _time.time() < deadline:
            try:
                resp = httpx.get(url, headers={"X-NBHD-Internal-Key": internal_key}, timeout=5)
                if resp.status_code < 500:
                    return True
            except Exception:
                pass
            _time.sleep(5)
        return False

    def _handle_container_restart(self, chat_id: int, tenant: Tenant, message_text: str) -> None:
        """Handle a container that's restarting — notify user and retry the message once it's back up."""
        if chat_id in self._update_in_progress:
            # Already handling a restart for this user — don't double-notify
            return
        self._update_in_progress.add(chat_id)
        tenant.refresh_from_db(fields=["status"])
        if tenant.status == "provisioning":
            self._send_message(
                chat_id,
                "Your assistant is almost ready — just finishing setup. "
                "I'll send your message through as soon as it's live! 🌱",
            )
        else:
            self._send_message(
                chat_id,
                "⏳ I'm restarting right now — I'll send your message through once I'm back up (about a minute).",
            )

        def _retry():
            try:
                if self._wait_for_container_ready(tenant, timeout=120):
                    context_msg = f"[System: assistant was restarting when this arrived. User's message:]\n{message_text}"
                    self._send_typing(chat_id)
                    self._forward_to_container(chat_id, tenant, context_msg)
                else:
                    self._send_message(chat_id, "I'm back! Sorry about that — could you resend your last message?")
            finally:
                self._update_in_progress.discard(chat_id)

        threading.Thread(target=_retry, daemon=True).start()

    def _forward_to_container(self, chat_id: int, tenant: Tenant, message_text: str) -> None:
        """Send the message to the tenant's OpenClaw container via /v1/chat/completions and relay the response."""
        if not tenant.container_fqdn or tenant.status == "provisioning":
            self._send_message(
                chat_id,
                "Your assistant is being set up — this usually takes about a minute. "
                "I'll be ready for you shortly! 🌱",
            )
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
            self._send_message(
                chat_id,
                "⏱️ That took longer than expected — I may have lost your message. Could you send it again?",
            )
            return
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response else 0
            logger.error("FWD_FAIL %s HTTP %s", tenant.container_fqdn, status_code)
            logger.error("FWD_BODY %s", (e.response.text[:300] if e.response else "none"))
            if status_code in (502, 503, 0):
                # Container restarting — buffer and retry
                self._handle_container_restart(chat_id, tenant, message_text)
            else:
                self._send_message(chat_id, "Something went wrong on my end. Please try again.")
            return
        except httpx.HTTPError as e:
            logger.error("Error forwarding to %s: %s", tenant.container_fqdn, e)
            self._handle_container_restart(chat_id, tenant, message_text)
            return
        finally:
            typing_stop.set()

        # Extract AI response text — retry once on empty
        ai_text = self._extract_ai_response(result)
        if not ai_text:
            logger.warning(
                "Empty response from container %s, retrying once",
                tenant.container_fqdn,
            )
            try:
                self._send_typing(chat_id)
                retry_resp = httpx.post(
                    url,
                    json={
                        "model": "openclaw",
                        "messages": [{"role": "user", "content": message_text}],
                        "user": str(chat_id),
                    },
                    headers={
                        "Authorization": f"Bearer {gateway_token}",
                        "X-User-Timezone": user_tz,
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
                "No response after retry from container %s for chat_id=%s",
                tenant.container_fqdn,
                chat_id,
            )
            self._send_message(
                chat_id,
                "Sorry, I couldn't come up with a response. "
                "Could you try saying that again?",
            )
            self._record_usage(tenant, result)
            return

        self._send_rich_response(chat_id, tenant, ai_text)

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
        usage = result.get("usage")
        if not isinstance(usage, dict) or not usage:
            model = result.get("model", "")
            if model:
                logger.warning(
                    "USAGE_MISSING tenant=%s model=%s result_keys=%s — "
                    "container returned a response but no usage object",
                    tenant.id, model, list(result.keys()),
                )
            else:
                logger.warning(
                    "USAGE_MISSING tenant=%s result_keys=%s — "
                    "no usage object and no model in response",
                    tenant.id, list(result.keys()),
                )
            return

        input_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
        output_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
        model_used = result.get("model", "")

        if not (input_tokens or output_tokens):
            logger.warning(
                "USAGE_ZERO tenant=%s model=%s usage_keys=%s — "
                "usage object present but token counts are zero",
                tenant.id, model_used, list(usage.keys()),
            )
            return

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

    def _handle_extraction_callback(self, update: dict, tenant: Tenant) -> None:
        """Handle PendingExtraction approve/dismiss callbacks."""
        from .extraction_callbacks import handle_extraction_callback

        try:
            import json as _json
            json_response = handle_extraction_callback(update, tenant)
            response_data = _json.loads(json_response.content)
            if response_data:
                self._execute_telegram_response(response_data)
        except Exception:
            logger.exception("Error handling extraction callback")
            callback_id = update["callback_query"].get("id")
            if callback_id:
                self._answer_callback_query(callback_id, "Something went wrong")

    def _handle_gate_callback(
        self,
        update: dict,
        tenant: Tenant,
        callback_data: str,
        callback_id: str,
        chat_id: int,
    ) -> None:
        """Handle action gate approve/deny callbacks."""
        from apps.actions.models import ActionStatus, PendingAction
        from apps.actions.messaging import update_gate_message
        from apps.actions.models import ActionAuditLog

        try:
            # Parse: gate_approve:123 or gate_deny:123
            parts = callback_data.split(":")
            if len(parts) != 2:
                self._answer_callback_query(callback_id, "Invalid callback")
                return

            action_str, action_id_str = parts[0], parts[1]
            is_approve = action_str == "gate_approve"
            action_id = int(action_id_str)

            action = PendingAction.objects.get(id=action_id, tenant=tenant)

            # Already resolved?
            if action.status != ActionStatus.PENDING:
                self._answer_callback_query(
                    callback_id, f"Already {action.status}"
                )
                return

            # Expired?
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
                self._answer_callback_query(callback_id, "⏰ Expired")
                return

            # Apply response
            from django.utils import timezone
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

            icon = "✅" if is_approve else "❌"
            label = "Approved" if is_approve else "Denied"
            self._answer_callback_query(callback_id, f"{icon} {label}")

            logger.info(
                "Gate callback: tenant=%s action=%s result=%s",
                tenant.id, action_id, action.status,
            )

        except PendingAction.DoesNotExist:
            self._answer_callback_query(callback_id, "Action not found")
        except Exception:
            logger.exception("Error handling gate callback")
            self._answer_callback_query(callback_id, "Something went wrong")

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
