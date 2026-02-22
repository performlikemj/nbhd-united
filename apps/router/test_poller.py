"""Tests for the central Telegram poller."""
from unittest.mock import MagicMock, patch, call
import json as _json

from django.test import TestCase, override_settings

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
        self.poller._http.post.return_value = MagicMock(
            is_success=True, json=MagicMock(return_value={"ok": True})
        )

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

    @patch("apps.router.poller.check_budget", return_value=False)
    @patch("apps.router.poller.resolve_tenant_by_chat_id")
    @patch("apps.router.poller.is_rate_limited", return_value=False)
    @patch("apps.router.poller.handle_start_command", return_value=None)
    def test_dispatch_budget_exhausted(self, mock_start, mock_rate, mock_resolve, mock_budget):
        tenant = MagicMock(spec=Tenant)
        tenant.status = Tenant.Status.ACTIVE
        tenant.is_trial = True
        tenant.monthly_token_budget = 100000
        tenant.tokens_this_month = 100000
        tenant.model_tier = Tenant.ModelTier.STARTER
        mock_resolve.return_value = tenant

        update = {"message": {"text": "hi", "chat": {"id": 222}}}
        self.poller._handle_update(update)

        # Should send budget exhausted message
        post_calls = self.poller._http.post.call_args_list
        self.assertTrue(len(post_calls) > 0)
        sent_json = post_calls[0][1].get("json", {})
        self.assertIn("token quota", sent_json.get("text", ""))

    @patch("apps.router.poller.record_usage")
    @patch("apps.router.poller.httpx.post")
    @patch("apps.router.poller.check_budget", return_value=True)
    @patch("apps.router.poller.resolve_tenant_by_chat_id")
    @patch("apps.router.poller.is_rate_limited", return_value=False)
    @patch("apps.router.poller.handle_start_command", return_value=None)
    def test_dispatch_forwards_to_container(
        self, mock_start, mock_rate, mock_resolve, mock_budget, mock_http_post, mock_usage
    ):
        import uuid
        tenant = MagicMock(spec=Tenant)
        tenant.id = uuid.uuid4()
        tenant.status = Tenant.Status.ACTIVE
        tenant.is_trial = True
        tenant.stripe_subscription_id = ""
        tenant.container_fqdn = "oc-test.internal"
        tenant.user.timezone = "UTC"
        mock_resolve.return_value = tenant

        # Mock the /v1/chat/completions response
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "model": "test",
            "choices": [{"message": {"content": "Hello from AI!"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        mock_http_post.return_value = mock_resp

        update = {"message": {"text": "what's up", "chat": {"id": 333}}}
        self.poller._handle_update(update)

        # Verify /v1/chat/completions was called
        http_calls = mock_http_post.call_args_list
        completions_call = [c for c in http_calls if "/v1/chat/completions" in str(c)]
        self.assertEqual(len(completions_call), 1)

        # Verify AI response was sent back via Telegram
        send_calls = self.poller._http.post.call_args_list
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
        self.assertIn("trial has ended", sent_json.get("text", ""))

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
    """Tests for TelegramPoller._forward_to_container."""

    def setUp(self):
        from apps.router.poller import TelegramPoller
        self.poller = TelegramPoller()
        self.poller._http = MagicMock()

    @patch("apps.router.poller.httpx.post")
    def test_forward_via_chat_completions(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "response"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }
        mock_post.return_value = mock_resp

        tenant = MagicMock()
        tenant.container_fqdn = "oc-test.internal"
        tenant.user.timezone = "UTC"

        self.poller._forward_to_container(123, tenant, "hi there")

        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        self.assertIn("/v1/chat/completions", url)
        self.assertIn("oc-test.internal", url)

    @patch("apps.router.poller.httpx.post")
    def test_forward_timeout_handled_gracefully(self, mock_post):
        import httpx
        mock_post.side_effect = httpx.TimeoutException("timed out")

        tenant = MagicMock()
        tenant.container_fqdn = "oc-test.internal"
        tenant.user.timezone = "UTC"

        # Should not raise
        self.poller._forward_to_container(123, tenant, "hi")

    @patch("apps.router.poller.httpx.post")
    def test_forward_error_sends_sorry_message(self, mock_post):
        import httpx
        mock_post.side_effect = httpx.HTTPError("server error")

        tenant = MagicMock()
        tenant.container_fqdn = "oc-test.internal"
        tenant.user.timezone = "UTC"

        self.poller._forward_to_container(123, tenant, "hi")

        # Should send error message to user
        send_calls = self.poller._http.post.call_args_list
        self.assertTrue(len(send_calls) > 0)


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

    def test_voice_message(self):
        update = {"message": {"voice": {"file_id": "abc"}}}
        self.assertIn("voice", self.poller._extract_message_text(update))

    def test_sticker(self):
        update = {"message": {"sticker": {"emoji": "😀"}}}
        result = self.poller._extract_message_text(update)
        self.assertIn("sticker", result)
        self.assertIn("😀", result)

    def test_no_message(self):
        self.assertIsNone(self.poller._extract_message_text({}))
