"""Tests for the admin-alert delivery classification used by run_health_check.

The classifier decides whether a failed alert POST should keep retrying every tick
(transient) or start the 30-minute cooldown (delivered / undeliverable) — the latter
is what stops a doomed Cloudflare-Access 302 from re-firing forever.
"""

from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from apps.cron.views import _send_alert_to_personal_openclaw


@override_settings(
    ADMIN_OPENCLAW_GATEWAY_URL="https://agent.example.com",
    ADMIN_OPENCLAW_GATEWAY_TOKEN="tok",
)
class SendAlertClassificationTest(TestCase):
    @patch("httpx.post")
    def test_200_is_delivered(self, mock_post):
        mock_post.return_value = Mock(status_code=200, text="ok")
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "delivered")

    @patch("httpx.post")
    def test_302_redirect_is_undeliverable(self, mock_post):
        # Cloudflare Access bounce to its login page — retrying won't help, so the
        # caller must start the cooldown (this is the 5-min-spam fix).
        mock_post.return_value = Mock(status_code=302, text="<html>302 Found</html>")
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "undeliverable")

    @patch("httpx.post")
    def test_4xx_is_undeliverable(self, mock_post):
        mock_post.return_value = Mock(status_code=403, text="forbidden")
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "undeliverable")

    @patch("httpx.post")
    def test_5xx_is_transient(self, mock_post):
        mock_post.return_value = Mock(status_code=503, text="busy")
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "transient")

    @patch("httpx.post", side_effect=Exception("network down"))
    def test_network_error_is_transient(self, _mock_post):
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "transient")

    @override_settings(ADMIN_OPENCLAW_GATEWAY_URL="", ADMIN_OPENCLAW_GATEWAY_TOKEN="")
    def test_unconfigured_is_undeliverable(self):
        # No spamming when the gateway isn't even configured.
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "undeliverable")
