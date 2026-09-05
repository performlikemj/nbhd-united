from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.steward.models import AlertState
from apps.steward.notify import send_urgent


class StewardNotifierTests(TestCase):
    @override_settings(STEWARD_ALERT_EMAIL="alerts@example.test")
    @patch("apps.steward.notify.send_mail", return_value=1)
    def test_email_success_records_confirmed_urgent(self, send_mail):
        result = send_urgent("Gateway missed", "Miss count: 1.", "steward-miss:7:1")

        self.assertEqual(result, "delivered")
        self.assertEqual(send_mail.call_args.kwargs["subject"], "[Steward] Gateway missed")
        state = AlertState.objects.get(fingerprint="steward-miss:7:1")
        self.assertIsNotNone(state.last_sent_at)
        self.assertEqual(state.sent_count, 1)

    @override_settings(STEWARD_ALERT_EMAIL="alerts@example.test")
    @patch("apps.steward.notify.send_mail", side_effect=RuntimeError("mail unavailable"))
    def test_email_failure_is_transient_and_not_recorded(self, _send_mail):
        with self.assertLogs("apps.steward.notify", level="ERROR"):
            result = send_urgent("Gateway missed", "Miss count: 1.", "steward-miss:7:1")

        self.assertEqual(result, "transient")
        self.assertFalse(AlertState.objects.exists())

    @override_settings(STEWARD_ALERT_EMAIL="")
    @patch("apps.steward.notify.send_mail")
    def test_nothing_configured_is_undeliverable(self, send_mail):
        with self.assertLogs("apps.steward.notify", level="ERROR"):
            result = send_urgent("Gateway missed", "Miss count: 1.", "steward-miss:7:1")

        self.assertEqual(result, "undeliverable")
        send_mail.assert_not_called()
        self.assertFalse(AlertState.objects.exists())
