"""Tests for the admin-alert delivery classification used by run_health_check.

The classifier decides whether a failed alert POST should keep retrying every tick
(transient), start a SHORT backoff (timeout — gateway slow/cold), or start the full
30-minute cooldown (delivered / undeliverable). The short-backoff path is what stops
a cold personal-OpenClaw gateway (read timeout or 5xx behind Cloudflare) from
re-firing — and Sentry-storming — every 5 minutes while never delivering.
"""

from unittest.mock import Mock, patch

import httpx
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.cron.views import (
    _HEALTH_ALERT_COOLDOWN_SECONDS,
    _HEALTH_ALERT_TIMEOUT_COOLDOWN_SECONDS,
    _send_alert_to_personal_openclaw,
)


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
    def test_503_is_timeout(self, mock_post):
        # A 5xx from a cold/waking gateway behind Cloudflare — back off ~10 min,
        # don't storm. (Previously classified 'transient'.)
        mock_post.return_value = Mock(status_code=503, text="busy")
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "timeout")

    @patch("httpx.post")
    def test_502_is_timeout(self, mock_post):
        mock_post.return_value = Mock(status_code=502, text="bad gateway")
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "timeout")

    @patch("httpx.post", side_effect=httpx.ReadTimeout("slow"))
    def test_read_timeout_is_timeout(self, _mock_post):
        # Connected fine, but the gateway's cold start + LLM round-trip blew the
        # read window. The gateway is warming — back off, don't hammer or go silent.
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "timeout")

    @patch("httpx.post", side_effect=httpx.ConnectTimeout("no route"))
    def test_connect_timeout_is_transient(self, _mock_post):
        # Couldn't even reach the gateway — fast failure, retry next tick.
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "transient")

    @patch("httpx.post", side_effect=httpx.ConnectError("refused"))
    def test_connect_error_is_transient(self, _mock_post):
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "transient")

    @patch("httpx.post", side_effect=Exception("network down"))
    def test_network_error_is_transient(self, _mock_post):
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "transient")

    @override_settings(ADMIN_OPENCLAW_GATEWAY_URL="", ADMIN_OPENCLAW_GATEWAY_TOKEN="")
    def test_unconfigured_is_undeliverable(self):
        # No spamming when the gateway isn't even configured.
        self.assertEqual(_send_alert_to_personal_openclaw("m"), "undeliverable")


@override_settings(DEPLOY_SECRET="health-secret")
class RunHealthCheckCooldownTest(TestCase):
    """The cooldown map is THE storm-stopping behaviour and must be covered:
    a 'timeout' result sets a short backoff, 'transient' leaves the cooldown unset,
    and 'delivered' uses the full cooldown."""

    _UNHEALTHY = [
        {
            "healthy": False,
            "display_name": "Down Tenant",
            "container": "oc-x",
            "checks": {},
            "error": "down",
        }
    ]

    @staticmethod
    def _health_set_calls(mock_set):
        return [c for c in mock_set.call_args_list if c.args and c.args[0] == "health_alert_sent"]

    def _run(self, alert_status):
        from django.core.cache import cache

        cache.delete("health_alert_sent")  # ensure no prior cooldown
        with (
            patch(
                "apps.orchestrator.services.check_all_tenants_health",
                return_value=self._UNHEALTHY,
            ),
            patch(
                "apps.cron.views._send_alert_to_personal_openclaw",
                return_value=alert_status,
            ),
            patch.object(cache, "set") as mock_set,
        ):
            resp = self.client.post(
                reverse("cron-run-health-check"),
                headers={"X-Deploy-Secret": "health-secret"},
            )
        return resp, mock_set

    def test_timeout_sets_short_backoff_cooldown(self):
        resp, mock_set = self._run("timeout")
        self.assertEqual(resp.status_code, 200)
        calls = self._health_set_calls(mock_set)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].args,
            ("health_alert_sent", True, _HEALTH_ALERT_TIMEOUT_COOLDOWN_SECONDS),
        )

    def test_transient_leaves_cooldown_unset(self):
        resp, mock_set = self._run("transient")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._health_set_calls(mock_set), [])

    def test_delivered_sets_full_cooldown(self):
        resp, mock_set = self._run("delivered")
        self.assertEqual(resp.status_code, 200)
        calls = self._health_set_calls(mock_set)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].args,
            ("health_alert_sent", True, _HEALTH_ALERT_COOLDOWN_SECONDS),
        )


@override_settings(DEPLOY_SECRET="health-secret")
class AdminHealthStatusTest(TestCase):
    """admin_health_status is the on-demand pull for MJ's personal OpenClaw
    ("how's NBHD?"). Hibernated tenants are healthy=True (asleep, not a fault) and
    must be broken out as a separate `hibernated` count, not conflated with serving."""

    def test_hibernated_count_breaks_out_asleep_from_serving(self):
        mixed = [
            {"healthy": True, "hibernated": True, "checks": {}},
            {"healthy": True, "checks": {}},
            {"healthy": False, "checks": {}, "error": "down"},
        ]
        with patch("apps.orchestrator.services.check_all_tenants_health", return_value=mixed):
            resp = self.client.get(
                reverse("cron-admin-health"),
                headers={"X-Deploy-Secret": "health-secret"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["healthy"], 2)  # serving + asleep both count as not-unhealthy
        self.assertEqual(body["unhealthy"], 1)
        self.assertEqual(body["hibernated"], 1)
