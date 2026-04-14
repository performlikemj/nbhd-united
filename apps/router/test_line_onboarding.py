"""Tests for the LINE onboarding gate."""

from unittest.mock import patch

from django.test import TestCase

from apps.router.line_flex import telegram_keyboard_to_quick_reply
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class TelegramKeyboardToQuickReplyTest(TestCase):
    def test_simple_keyboard(self):
        keyboard = [
            [{"text": "Japan", "callback_data": "tz_country:Japan"}],
            [{"text": "USA", "callback_data": "tz_country:USA"}],
        ]
        items = telegram_keyboard_to_quick_reply(keyboard)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["action"]["label"], "Japan")
        self.assertEqual(items[0]["action"]["data"], "tz_country:Japan")
        self.assertEqual(items[0]["type"], "action")
        self.assertEqual(items[0]["action"]["type"], "postback")

    def test_flattens_multi_column_rows(self):
        keyboard = [
            [
                {"text": "A", "callback_data": "a"},
                {"text": "B", "callback_data": "b"},
            ],
            [{"text": "C", "callback_data": "c"}],
        ]
        items = telegram_keyboard_to_quick_reply(keyboard)
        self.assertEqual(len(items), 3)
        labels = [i["action"]["label"] for i in items]
        self.assertEqual(labels, ["A", "B", "C"])

    def test_truncates_label_to_20_chars(self):
        keyboard = [
            [{"text": "Eastern (NYC, Miami, Atlanta)", "callback_data": "tz_zone:America/New_York"}],
        ]
        items = telegram_keyboard_to_quick_reply(keyboard)
        self.assertLessEqual(len(items[0]["action"]["label"]), 20)
        # displayText keeps full text
        self.assertEqual(items[0]["action"]["displayText"], "Eastern (NYC, Miami, Atlanta)")

    def test_max_13_items(self):
        keyboard = [[{"text": f"Item {i}", "callback_data": f"d{i}"}] for i in range(20)]
        items = telegram_keyboard_to_quick_reply(keyboard)
        self.assertEqual(len(items), 13)

    def test_empty_keyboard(self):
        self.assertEqual(telegram_keyboard_to_quick_reply([]), [])


class LineOnboardingGateTest(TestCase):
    """Test that the onboarding gate intercepts LINE messages for incomplete onboarding."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Friend",
            telegram_chat_id=99999,
        )
        # Simulate LINE-only user
        self.tenant.user.line_user_id = "U_line_test_123"
        self.tenant.user.save(update_fields=["line_user_id"])
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_fqdn = "test.example.com"
        self.tenant.save(update_fields=["status", "container_fqdn"])

    @patch("apps.router.line_webhook._send_line_push")
    @patch("apps.router.line_webhook._resolve_tenant_by_line_user_id")
    def test_onboarding_incomplete_intercepts_message(self, mock_resolve, mock_push):
        """Tenant with onboarding_complete=False should NOT reach the container."""
        mock_resolve.return_value = self.tenant
        mock_push.return_value = True

        from apps.router.line_webhook import LineWebhookView

        view = LineWebhookView()

        event = {
            "type": "message",
            "source": {"userId": "U_line_test_123"},
            "message": {"type": "text", "text": "hello"},
            "replyToken": "dummy_token",
        }

        with patch.object(view, "_forward_to_container") as mock_forward:
            view._handle_message(event)
            mock_forward.assert_not_called()

        # Should have sent an onboarding reply
        mock_push.assert_called()

    @patch("apps.router.line_webhook.LineWebhookView._forward_to_container")
    @patch("apps.router.line_webhook._show_loading")
    @patch("apps.router.line_webhook._send_line_flex")
    @patch("apps.router.line_webhook.check_budget", return_value="")
    @patch("apps.router.line_webhook._send_line_push")
    @patch("apps.router.line_webhook._resolve_tenant_by_line_user_id")
    def test_onboarding_complete_passes_through(
        self, mock_resolve, mock_push, mock_budget, mock_flex, mock_loading, mock_forward
    ):
        """Tenant with onboarding_complete=True should reach the container."""
        self.tenant.onboarding_complete = True
        self.tenant.onboarding_step = 5
        self.tenant.save(update_fields=["onboarding_complete", "onboarding_step"])
        # Set real profile data so needs_reintroduction returns False
        self.tenant.user.display_name = "Yuki"
        self.tenant.user.timezone = "Asia/Tokyo"
        self.tenant.user.language = "ja"
        self.tenant.user.save(update_fields=["display_name", "timezone", "language"])
        mock_resolve.return_value = self.tenant
        mock_push.return_value = True

        from apps.router.line_webhook import LineWebhookView

        view = LineWebhookView()

        event = {
            "type": "message",
            "source": {"userId": "U_line_test_123"},
            "message": {"type": "text", "text": "help me plan food"},
            "replyToken": "dummy_token",
        }

        view._handle_message(event)
        mock_forward.assert_called_once()

    @patch("apps.router.line_webhook._send_line_flex")
    @patch("apps.router.line_webhook._resolve_tenant_by_line_user_id")
    def test_sticker_during_onboarding_gets_nudge(self, mock_resolve, mock_flex):
        """Stickers during onboarding should get a 'please type' message."""
        self.tenant.onboarding_step = 1  # Waiting for name
        self.tenant.save(update_fields=["onboarding_step"])
        mock_resolve.return_value = self.tenant
        mock_flex.return_value = True

        from apps.router.line_webhook import LineWebhookView

        view = LineWebhookView()

        event = {
            "type": "message",
            "source": {"userId": "U_line_test_123"},
            "message": {
                "type": "sticker",
                "packageId": "1",
                "stickerId": "1",
                "stickerResourceType": "STATIC",
            },
            "replyToken": "dummy_token",
        }

        with patch.object(view, "_forward_to_container") as mock_forward:
            view._handle_message(event)
            mock_forward.assert_not_called()

        # Should have sent a nudge via _send_line_flex
        mock_flex.assert_called()
        flex_msg = mock_flex.call_args[0][1]
        # Check that it's a short bubble with a "type your answer" message
        self.assertIn("type", str(flex_msg))


class LineOnboardingPostbackTest(TestCase):
    """Test that onboarding postback callbacks are handled."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Yuki",
            telegram_chat_id=99998,
        )
        self.tenant.user.line_user_id = "U_line_postback_test"
        self.tenant.user.save(update_fields=["line_user_id"])
        self.tenant.onboarding_step = 3  # Waiting for country
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["onboarding_step", "status"])

    @patch("apps.router.line_webhook._send_line_push")
    @patch("apps.router.line_webhook._resolve_tenant_by_line_user_id")
    def test_country_postback_handled(self, mock_resolve, mock_push):
        """tz_country postback should trigger onboarding callback."""
        mock_resolve.return_value = self.tenant
        mock_push.return_value = True

        from apps.router.line_webhook import LineWebhookView

        view = LineWebhookView()

        event = {
            "type": "postback",
            "source": {"userId": "U_line_postback_test"},
            "postback": {"data": "tz_country:Japan"},
        }
        view._handle_postback(event)

        # Should have sent an onboarding reply
        mock_push.assert_called()
        # Tenant should have advanced (Japan is single-timezone → step 4)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.onboarding_step, 4)
