from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from apps.steward.notify import send_digest, send_urgent


class StewardNotifierTests(SimpleTestCase):
    @override_settings(
        STEWARD_TELEGRAM_BOT_TOKEN="fake-telegram-token",
        STEWARD_TELEGRAM_CHAT_ID="fake-chat-id",
        STEWARD_ALERT_EMAIL="fallback@example.test",
    )
    @patch("apps.steward.notify.send_mail")
    @patch("apps.steward.notify.requests.post")
    def test_telegram_success(self, post, send_mail):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"ok": True}

        result = send_urgent("Gateway missed", "Miss count: 1.", "miss:1")

        self.assertEqual(result, "delivered")
        post.assert_called_once()
        send_mail.assert_not_called()

    @override_settings(
        STEWARD_TELEGRAM_BOT_TOKEN="fake-telegram-token",
        STEWARD_TELEGRAM_CHAT_ID="fake-chat-id",
        STEWARD_ALERT_EMAIL="fallback@example.test",
    )
    @patch("apps.steward.notify.send_mail", return_value=1)
    @patch("apps.steward.notify.requests.post")
    def test_telegram_failure_uses_mailgun_fallback(self, post, send_mail):
        post.return_value = Mock(status_code=503, text="unavailable")

        result = send_urgent("Gateway missed", "Miss count: 1.", "miss:1")

        self.assertEqual(result, "delivered")
        send_mail.assert_called_once()

    @override_settings(
        STEWARD_TELEGRAM_BOT_TOKEN="",
        STEWARD_TELEGRAM_CHAT_ID="",
        STEWARD_ALERT_EMAIL="",
    )
    @patch("apps.steward.notify.send_mail")
    @patch("apps.steward.notify.requests.post")
    def test_nothing_configured_is_undeliverable(self, post, send_mail):
        result = send_urgent("Gateway missed", "Miss count: 1.", "miss:1")

        self.assertEqual(result, "undeliverable")
        post.assert_not_called()
        send_mail.assert_not_called()

    @override_settings(
        STEWARD_TELEGRAM_BOT_TOKEN="fake-telegram-token",
        STEWARD_TELEGRAM_CHAT_ID="fake-chat-id",
        STEWARD_ALERT_EMAIL="fallback@example.test",
    )
    @patch("apps.steward.notify.send_mail")
    @patch("apps.steward.notify.requests.post")
    def test_digest_telegram_is_plain_text_without_urgency_framing(
        self,
        post,
        send_mail,
    ):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"ok": True}

        self.assertEqual(send_digest("STEWARD DAILY FACTS"), "delivered")

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["text"], "STEWARD DAILY FACTS")
        self.assertNotIn("parse_mode", payload)
        send_mail.assert_not_called()

    @override_settings(
        STEWARD_TELEGRAM_BOT_TOKEN="",
        STEWARD_TELEGRAM_CHAT_ID="",
        STEWARD_ALERT_EMAIL="fallback@example.test",
    )
    @patch("apps.steward.notify.send_mail", return_value=1)
    def test_digest_fallback_subject(self, send_mail):
        self.assertEqual(send_digest("facts"), "delivered")
        self.assertEqual(send_mail.call_args.kwargs["subject"], "[Steward digest]")
