"""Tests for the central Telegram poller."""

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.router.poller import TelegramPoller
from apps.tenants.models import Tenant


@override_settings(
    TELEGRAM_BOT_TOKEN="TEST-BOT-TOKEN",
    TELEGRAM_WEBHOOK_SECRET="test-secret",
    FRONTEND_URL="https://app.example.com",
    ROUTER_RATE_LIMIT_PER_MINUTE=10,
)
class TelegramPollerInitTest(TestCase):
    """TelegramPoller.__init__ and startup tests."""

    def test_init_sets_token(self):
        from apps.router.poller import TelegramPoller

        poller = TelegramPoller()
        self.assertEqual(poller.bot_token, "TEST-BOT-TOKEN")
        self.assertEqual(poller.offset, 0)
        self.assertFalse(poller._running)

    @patch("apps.router.poller.httpx.post")
    def test_delete_webhook_on_startup(self, mock_post):
        from apps.router.poller import TelegramPoller

        mock_post.return_value = MagicMock(
            json=MagicMock(return_value={"ok": True, "description": "Webhook was deleted"})
        )
        poller = TelegramPoller()
        poller._delete_webhook()
        mock_post.assert_called_once()
        self.assertIn("deleteWebhook", mock_post.call_args[0][0])


@override_settings(
    TELEGRAM_BOT_TOKEN="TEST-BOT-TOKEN",
    TELEGRAM_WEBHOOK_SECRET="test-secret",
    FRONTEND_URL="https://app.example.com",
    ROUTER_RATE_LIMIT_PER_MINUTE=10,
)
class TelegramPollerDispatchTest(TestCase):
    """Tests for TelegramPoller._handle_update."""

    def setUp(self):
        from apps.router.poller import TelegramPoller
        from apps.router.services import clear_cache, clear_rate_limits

        clear_cache()
        clear_rate_limits()
        self.poller = TelegramPoller()
        # Give it a mock http client
        self.poller._http = MagicMock()
        self.poller._http.post.return_value = MagicMock(is_success=True, json=MagicMock(return_value={"ok": True}))

    def tearDown(self):
        from apps.router.services import clear_cache, clear_rate_limits

        clear_cache()
        clear_rate_limits()

    @patch("apps.router.poller.handle_start_command")
    def test_dispatch_start_command(self, mock_start):
        mock_start.return_value = {
            "method": "sendMessage",
            "chat_id": 12345,
            "text": "Linked!",
        }
        update = {
            "message": {
                "text": "/start abc123",
                "chat": {"id": 12345},
                "from": {"id": 99, "username": "tester", "first_name": "Test"},
            },
        }
        self.poller._handle_update(update)
        mock_start.assert_called_once_with(update)
        # Should have called sendMessage via _execute_telegram_response
        self.poller._http.post.assert_called()

    @patch("apps.router.poller.resolve_tenant_by_chat_id", return_value=None)
    @patch("apps.router.poller.handle_start_command", return_value=None)
    def test_dispatch_unknown_user_sends_onboarding(self, mock_start, mock_resolve):
        update = {"message": {"text": "hello", "chat": {"id": 999000}}}
        self.poller._handle_update(update)
        # Should have sent onboarding link
        post_calls = self.poller._http.post.call_args_list
        self.assertTrue(any("sendMessage" in str(c) for c in post_calls))

    @patch("apps.router.poller.is_rate_limited", return_value=True)
    @patch("apps.router.poller.handle_start_command", return_value=None)
    def test_dispatch_rate_limited_skips(self, mock_start, mock_rate):
        update = {"message": {"text": "spam", "chat": {"id": 111}}}
        self.poller._handle_update(update)
        # Rate-limited: no Telegram API calls
        self.poller._http.post.assert_not_called()

    @patch("apps.router.poller.check_budget", return_value="personal")
    @patch("apps.router.poller.resolve_tenant_by_chat_id")
    @patch("apps.router.poller.is_rate_limited", return_value=False)
    @patch("apps.router.poller.handle_start_command", return_value=None)
    def test_dispatch_budget_exhausted(self, mock_start, mock_rate, mock_resolve, mock_budget):
        tenant = MagicMock(spec=Tenant)
        tenant.status = Tenant.Status.ACTIVE
        tenant.is_trial = True
        tenant.effective_cost_budget = 5
        tenant.estimated_cost_this_month = 5
        tenant.model_tier = Tenant.ModelTier.STARTER
        tenant.user = MagicMock(language="en")
        mock_resolve.return_value = tenant

        update = {"message": {"text": "hi", "chat": {"id": 222}}}
        self.poller._handle_update(update)

        # Should send budget exhausted message
        post_calls = self.poller._http.post.call_args_list
        self.assertTrue(len(post_calls) > 0)
        sent_json = post_calls[0][1].get("json", {})
        self.assertIn("free trial allowance", sent_json.get("text", ""))

    @patch("apps.router.pending_queue.record_usage")
    @patch("apps.router.pending_queue.httpx.post")
    @patch("apps.router.poller.check_budget", return_value="")
    @patch("apps.router.poller.resolve_tenant_by_chat_id")
    @patch("apps.router.poller.is_rate_limited", return_value=False)
    @patch("apps.router.poller.handle_start_command", return_value=None)
    def test_dispatch_forwards_to_container(
        self, mock_start, mock_rate, mock_resolve, mock_budget, mock_http_post, mock_usage
    ):
        # Real tenant — PR #431 routes the forward through PendingMessage,
        # which has an FK on Tenant that won't accept a MagicMock. Profile
        # fields are populated so ``needs_reintroduction`` returns False
        # and we land in the actual forward path instead of onboarding.
        import secrets

        from apps.tenants.models import Tenant, User

        user = User.objects.create_user(
            username=f"poller_disp_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=333,
            preferred_channel="telegram",
            display_name="Test Person",
            timezone="America/Los_Angeles",
            language="en",
            preferences={"onboarding_interests": "anything"},
        )
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-test.internal",
        )
        tenant.onboarding_complete = True
        tenant.onboarding_step = 999
        tenant.save(update_fields=["onboarding_complete", "onboarding_step"])
        mock_resolve.return_value = tenant

        # Mock the /v1/chat/completions response (queue drain uses
        # pending_queue.httpx.post now, plus typing/sendMessage calls).
        def _route(url, *args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.is_success = True
            mock_resp.status_code = 200
            if "/v1/chat/completions" in url:
                mock_resp.json.return_value = {
                    "choices": [{"message": {"content": "Hello from AI!"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                }
            else:
                mock_resp.json.return_value = {"ok": True}
            return mock_resp

        mock_http_post.side_effect = _route

        update = {"message": {"text": "what's up", "chat": {"id": 333}}}
        self.poller._handle_update(update)

        # Verify /v1/chat/completions was called via the queue drain.
        http_calls = mock_http_post.call_args_list
        completions_call = [c for c in http_calls if "/v1/chat/completions" in str(c)]
        self.assertEqual(len(completions_call), 1)

        # AI response was relayed back via the queue's Telegram Bot API
        # call (sendMessage), not the poller's _http.post.
        send_calls = [c for c in http_calls if "sendMessage" in str(c)]
        self.assertTrue(len(send_calls) > 0)

    @patch("apps.router.poller.handle_start_command", return_value=None)
    def test_dispatch_no_chat_id_returns_early(self, mock_start):
        update = {}  # No message, no callback_query
        self.poller._handle_update(update)
        self.poller._http.post.assert_not_called()

    @patch("apps.router.poller.resolve_tenant_by_chat_id")
    @patch("apps.router.poller.is_rate_limited", return_value=False)
    @patch("apps.router.poller.handle_start_command", return_value=None)
    def test_dispatch_suspended_tenant_gets_subscribe_link(self, mock_start, mock_rate, mock_resolve):
        tenant = MagicMock(spec=Tenant)
        tenant.status = Tenant.Status.SUSPENDED
        tenant.is_trial = False
        tenant.stripe_subscription_id = ""
        mock_resolve.return_value = tenant

        update = {"message": {"text": "hey", "chat": {"id": 555}}}
        self.poller._handle_update(update)

        post_calls = self.poller._http.post.call_args_list
        self.assertTrue(len(post_calls) > 0)
        sent_json = post_calls[0][1].get("json", {})
        self.assertIn("assistant is paused", sent_json.get("text", ""))

    @patch("apps.router.poller.TelegramPoller._handle_lesson_callback")
    @patch("apps.router.poller.resolve_tenant_by_chat_id")
    @patch("apps.router.poller.is_rate_limited", return_value=False)
    @patch("apps.router.poller.handle_start_command", return_value=None)
    def test_dispatch_lesson_callback(self, mock_start, mock_rate, mock_resolve, mock_lesson):
        tenant = MagicMock(spec=Tenant)
        tenant.status = Tenant.Status.ACTIVE
        mock_resolve.return_value = tenant

        update = {
            "callback_query": {
                "id": "cb1",
                "data": "lesson:approve:42",
                "message": {"chat": {"id": 666}},
                "from": {"id": 99},
            },
        }
        self.poller._handle_update(update)
        mock_lesson.assert_called_once_with(update, tenant)


@override_settings(
    TELEGRAM_BOT_TOKEN="TEST-BOT-TOKEN",
    TELEGRAM_WEBHOOK_SECRET="test-secret",
)
class TelegramPollerForwardTest(TestCase):
    """Tests for TelegramPoller._forward_to_container.

    After PR #431 (per-tenant queue), the poller's
    ``_forward_to_container`` enqueues onto a serialized queue and the
    QStash drain task does the actual POST. In tests, ``publish_task``
    falls back to synchronous execution (no QSTASH_TOKEN), so the drain
    runs inline and the patched ``httpx.post`` still observes the
    container POST — just from ``pending_queue.httpx.post`` instead of
    ``poller.httpx.post``.
    """

    def setUp(self):
        import secrets

        from apps.router.poller import TelegramPoller
        from apps.tenants.models import Tenant, User

        self.poller = TelegramPoller()
        self.poller._http = MagicMock()
        self.poller._http.post.return_value = MagicMock(is_success=True)

        # Real tenant — PendingMessage.objects.create requires a real
        # FK, MagicMock won't survive Django's validation.
        self.user = User.objects.create_user(
            username=f"poller_fwd_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=123,
            preferred_channel="telegram",
        )
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-test.internal",
        )

    @patch("apps.router.pending_queue.httpx.post")
    def test_forward_via_chat_completions(self, mock_post):
        # The queue's drain makes several POSTs: a Telegram typing pulse,
        # the /v1/chat/completions to the container, and a sendMessage to
        # deliver the AI reply. We assert the chat-completions call was
        # made exactly once with the right payload.
        def _route(url, *args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.is_success = True
            mock_resp.status_code = 200
            if "/v1/chat/completions" in url:
                mock_resp.json.return_value = {
                    "choices": [{"message": {"content": "AI response"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 10},
                }
            else:
                mock_resp.json.return_value = {"ok": True}
            return mock_resp

        mock_post.side_effect = _route

        self.poller._forward_to_container(123, self.tenant, "hi there")

        # Exactly one chat-completions POST went to the tenant container.
        completions_calls = [c for c in mock_post.call_args_list if "/v1/chat/completions" in c.args[0]]
        self.assertEqual(len(completions_calls), 1)
        url = completions_calls[0].args[0]
        self.assertIn("oc-test.internal", url)

        # Verify chat completions payload
        payload = completions_calls[0].kwargs["json"]
        self.assertEqual(payload["model"], "openclaw")
        content = payload["messages"][0]["content"]
        # Time header is injected before the user message
        self.assertIn("[Now: ", content)
        # Conversational-turn marker tells the agent to skip the heavy
        # AGENTS.md auto-context-load (huge for cold-start BYO Claude).
        self.assertIn("[chat:", content)
        self.assertTrue(content.endswith("hi there"))

        # Verify AI response was relayed back via Telegram Bot API
        # (sendMessage call from the queue's drain).
        send_calls = [c for c in mock_post.call_args_list if "sendMessage" in c.args[0]]
        self.assertTrue(len(send_calls) > 0)

    @patch("apps.router.pending_queue.httpx.post")
    def test_forward_timeout_handled_gracefully(self, mock_post):
        import httpx

        # Drain task will raise; enqueue_message_for_tenant swallows
        # publish failures so the webhook handler stays clean.
        mock_post.side_effect = httpx.TimeoutException("timed out")

        # Should not raise — enqueue swallows publish failures.
        self.poller._forward_to_container(123, self.tenant, "hi")

    @patch("apps.router.pending_queue.httpx.post")
    def test_forward_error_does_not_raise(self, mock_post):
        import httpx

        mock_post.side_effect = httpx.HTTPError("server error")

        # Drain raises but enqueue swallows it — caller (poller) stays
        # clean and the row sits at delivery_attempts=1 for the next
        # tick to retry.
        self.poller._forward_to_container(123, self.tenant, "hi")


@override_settings(TELEGRAM_BOT_TOKEN="TEST-BOT-TOKEN")
class TelegramPollerSendMessageTest(TestCase):
    """Tests for TelegramPoller._send_message."""

    def setUp(self):
        from apps.router.poller import TelegramPoller

        self.poller = TelegramPoller()
        self.poller._http = MagicMock()
        self.poller._http.post.return_value = MagicMock(is_success=True)

    def test_send_message_calls_telegram_api(self):
        self.poller._send_message(12345, "Hello there!")
        self.poller._http.post.assert_called_once()
        call_args = self.poller._http.post.call_args
        url = call_args[0][0]
        self.assertIn("sendMessage", url)
        payload = call_args[1]["json"]
        self.assertEqual(payload["chat_id"], 12345)
        self.assertEqual(payload["text"], "Hello there!")

    def test_send_message_handles_failure(self):
        import httpx

        self.poller._http.post.side_effect = httpx.HTTPError("API down")
        # Should not raise
        self.poller._send_message(12345, "Hello there!")


class TelegramPollerExtractTextTest(TestCase):
    """Tests for _extract_message_text."""

    def setUp(self):
        from apps.router.poller import TelegramPoller

        self.poller = TelegramPoller.__new__(TelegramPoller)

    def test_text_message(self):
        update = {"message": {"text": "hello"}}
        self.assertEqual(self.poller._extract_message_text(update), "hello")

    def test_photo_with_caption(self):
        update = {"message": {"photo": [{}], "caption": "look at this"}}
        self.assertEqual(self.poller._extract_message_text(update), "look at this")

    def test_photo_without_caption(self):
        update = {"message": {"photo": [{}]}}
        self.assertIn("photo", self.poller._extract_message_text(update))

    @patch.object(TelegramPoller, "_transcribe_voice", return_value="hello")
    def test_voice_message_transcribed(self, _mock):
        update = {"message": {"voice": {"file_id": "abc"}}}
        result = self.poller._extract_message_text(update)
        self.assertIn("hello", result)
        self.assertIn("🎤", result)

    @patch.object(TelegramPoller, "_transcribe_voice", return_value=None)
    def test_voice_message_fallback(self, _mock):
        update = {"message": {"voice": {"file_id": "abc"}}}
        result = self.poller._extract_message_text(update)
        self.assertIn("couldn't transcribe", result)

    def test_sticker(self):
        update = {"message": {"sticker": {"emoji": "😀"}}}
        result = self.poller._extract_message_text(update)
        self.assertIn("sticker", result)
        self.assertIn("😀", result)

    def test_no_message(self):
        self.assertIsNone(self.poller._extract_message_text({}))


# ────────────────────────────────────────────────────────────────────────────
# Localization of forward-error system messages (PR #394)
# ────────────────────────────────────────────────────────────────────────────


@override_settings(
    TELEGRAM_BOT_TOKEN="TEST-BOT-TOKEN",
    TELEGRAM_WEBHOOK_SECRET="test-secret",
    FRONTEND_URL="https://app.example.com",
    ROUTER_RATE_LIMIT_PER_MINUTE=10,
)
class TelegramPollerForwardErrorLocalizationTest(TestCase):
    """The user-facing notices the Telegram poller emits when the upstream
    container is unhappy must respect tenant.user.language. Telegram-specific
    keys (telegram_restarting, telegram_provisioning_almost_ready,
    telegram_resend_after_failed_wait) preserve the auto-retry promise that
    LINE doesn't share."""

    def setUp(self):
        import secrets

        from apps.tenants.services import create_tenant

        # Distinct chat_id per test to avoid in-memory _update_in_progress collisions.
        self.chat_id = 100000 + secrets.randbits(20)
        self.tenant = create_tenant(
            display_name="Loc Test",
            telegram_chat_id=self.chat_id,
        )
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_fqdn = "oc-loc-test.example.com"
        self.tenant.save(update_fields=["status", "container_fqdn"])

        self.poller = TelegramPoller()
        self.poller._http = MagicMock()
        self.poller._send_message = MagicMock()
        self.poller._answer_callback_query = MagicMock()

    def _set_language(self, lang: str) -> None:
        self.tenant.user.language = lang
        self.tenant.user.save(update_fields=["language"])
        self.tenant.refresh_from_db()

    def _last_send_message_text(self) -> str:
        # _send_message(chat_id, text)
        return self.poller._send_message.call_args[0][1]

    @patch("apps.router.poller.threading.Thread")
    def test_handle_container_restart_running_localized_to_japanese(self, _mock_thread):
        """5xx on a running container should send the JP telegram_restarting
        message that promises auto-retry, not the LINE-style 'try again'
        wording or hardcoded English."""
        self._set_language("ja")

        self.poller._handle_container_restart(self.chat_id, self.tenant, "hello")

        body = self._last_send_message_text()
        # Japanese marker for "restarting" — \u518d\u8d77\u52d5
        self.assertIn("\u518d\u8d77\u52d5", body)
        # Auto-retry promise must be present (Telegram-specific behavior)
        # \u9001\u4fe1 = "send"
        self.assertIn("\u9001\u4fe1", body)
        # English markers must NOT leak
        self.assertNotIn("I'm restarting", body)

    @patch("apps.router.poller.threading.Thread")
    def test_handle_container_restart_provisioning_localized_to_japanese(self, _mock_thread):
        self._set_language("ja")
        self.tenant.status = Tenant.Status.PROVISIONING
        self.tenant.save(update_fields=["status"])

        self.poller._handle_container_restart(self.chat_id, self.tenant, "hello")

        body = self._last_send_message_text()
        # JP "almost ready" / "setup finishing" — \u30bb\u30c3\u30c8\u30a2\u30c3\u30d7
        self.assertIn("\u30bb\u30c3\u30c8\u30a2\u30c3\u30d7", body)
        self.assertNotIn("almost ready", body)

    def test_forward_to_container_provisioning_setup_localized_to_japanese(self):
        self._set_language("ja")
        self.tenant.container_fqdn = ""  # → triggers provisioning_setup branch
        self.tenant.save(update_fields=["container_fqdn"])

        self.poller._forward_to_container(self.chat_id, self.tenant, "hi")

        body = self._last_send_message_text()
        self.assertIn("\u30bb\u30c3\u30c8\u30a2\u30c3\u30d7", body)
        self.assertNotIn("being set up", body)

    @patch("apps.router.poller.threading.Thread")
    def test_handle_container_restart_falls_back_to_english(self, _mock_thread):
        """Untranslated languages fall back to the English form of the
        telegram_restarting key (still has auto-retry promise)."""
        self._set_language("vi")  # Vietnamese — not translated

        self.poller._handle_container_restart(self.chat_id, self.tenant, "hello")

        body = self._last_send_message_text()
        # English fallback retains the auto-retry promise.
        self.assertIn("I'm restarting right now", body)
        self.assertIn("send your message through", body)
