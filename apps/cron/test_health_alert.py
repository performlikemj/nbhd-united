"""Tests for the health-alert delivery classification used by run_health_check.

Alerts push straight to MJ's phone via Pushover (a single HTTPS POST — no
Cloudflare tunnel, no personal gateway, no agent). The classifier decides
whether a failed POST keeps retrying every tick (transient), starts a SHORT
backoff (timeout — Pushover briefly down), or takes the full 30-minute cooldown
(delivered / undeliverable). The status vocabulary is unchanged from the old
gateway sender, so the caller's cooldown map is untouched.
"""

from unittest.mock import Mock, patch

import httpx
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.cron.views import (
    _HEALTH_ALERT_COOLDOWN_SECONDS,
    _HEALTH_ALERT_TIMEOUT_COOLDOWN_SECONDS,
    _send_alert_via_pushover,
)


@override_settings(PUSHOVER_API_TOKEN="tok", PUSHOVER_USER_KEY="usr")
class SendAlertClassificationTest(TestCase):
    @patch("httpx.post")
    def test_200_is_delivered(self, mock_post):
        mock_post.return_value = Mock(status_code=200, text='{"status":1}')
        self.assertEqual(_send_alert_via_pushover("m"), "delivered")

    @patch("httpx.post")
    def test_posts_to_pushover_with_credentials(self, mock_post):
        mock_post.return_value = Mock(status_code=200, text='{"status":1}')
        _send_alert_via_pushover("hello")
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://api.pushover.net/1/messages.json")
        data = kwargs["data"]
        self.assertEqual(data["token"], "tok")
        self.assertEqual(data["user"], "usr")
        self.assertEqual(data["message"], "hello")
        self.assertEqual(data["priority"], 1)  # high — bypasses quiet hours

    @patch("httpx.post")
    def test_4xx_is_undeliverable(self, mock_post):
        # Bad token/user/params — retrying won't fix it, take the full cooldown.
        mock_post.return_value = Mock(status_code=400, text='{"status":0,"errors":["application token is invalid"]}')
        self.assertEqual(_send_alert_via_pushover("m"), "undeliverable")

    @patch("httpx.post")
    def test_429_quota_is_undeliverable(self, mock_post):
        # Monthly message quota exhausted — retrying this cycle can't help.
        mock_post.return_value = Mock(status_code=429, text="rate limited")
        self.assertEqual(_send_alert_via_pushover("m"), "undeliverable")

    @patch("httpx.post")
    def test_500_is_timeout(self, mock_post):
        # Pushover briefly down — short backoff, don't storm.
        mock_post.return_value = Mock(status_code=500, text="server error")
        self.assertEqual(_send_alert_via_pushover("m"), "timeout")

    @patch("httpx.post", side_effect=httpx.ReadTimeout("slow"))
    def test_read_timeout_is_timeout(self, _mock_post):
        self.assertEqual(_send_alert_via_pushover("m"), "timeout")

    @patch("httpx.post", side_effect=httpx.ConnectTimeout("no route"))
    def test_connect_timeout_is_transient(self, _mock_post):
        self.assertEqual(_send_alert_via_pushover("m"), "transient")

    @patch("httpx.post", side_effect=httpx.ConnectError("refused"))
    def test_connect_error_is_transient(self, _mock_post):
        self.assertEqual(_send_alert_via_pushover("m"), "transient")

    @patch("httpx.post", side_effect=Exception("network down"))
    def test_network_error_is_transient(self, _mock_post):
        self.assertEqual(_send_alert_via_pushover("m"), "transient")

    @override_settings(PUSHOVER_API_TOKEN="", PUSHOVER_USER_KEY="")
    def test_unconfigured_is_undeliverable(self):
        # No spamming when Pushover isn't configured.
        self.assertEqual(_send_alert_via_pushover("m"), "undeliverable")


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
                "apps.cron.views._send_alert_via_pushover",
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
