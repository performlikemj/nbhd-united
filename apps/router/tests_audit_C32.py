"""Audit regression tests for FA-0951 (cluster C32).

The 20-messages/hour cap in CronDeliveryView is checked for every channel but
historically only incremented for Telegram/LINE — the app (iOS-only) channel
built its 2xx Response inline and never touched the counter, so the throttle was
silently a no-op for App Store installs. These tests drive sends through the app
channel end-to-end to prove the counter now increments and the cap trips.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.router.cron_delivery import RATE_LIMIT_PER_HOUR, _rate_counts
from apps.router.models import DeviceToken
from apps.tenants.models import Tenant


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class AppChannelRateLimitTest(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        # No Telegram/LINE link -> resolve_user_channel falls back to "app".
        self.user = User.objects.create_user(username="iosonly", password="pass")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
        )
        # A registered device makes the app channel the delivery surface.
        DeviceToken.objects.create(tenant=self.tenant, user=self.user, token="a" * 64)
        self.client = APIClient()
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/"
        _rate_counts.clear()

    def _headers(self):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _send(self, text="proactive ping"):
        # record_proactive_outbound dispatches an APNs push; stub it so the test
        # exercises only the rate-limit accounting, not the push transport.
        with patch("apps.router.proactive_context.record_proactive_outbound"):
            return self.client.post(self.url, {"message": text}, format="json", **self._headers())

    def test_app_channel_send_increments_counter(self):
        resp = self._send()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["channel"], "app")
        # The hourly counter must have advanced by exactly one for this tenant.
        self.assertEqual(len(_rate_counts.get(str(self.tenant.id), [])), 1)

    def test_app_channel_send_increments_once_per_call(self):
        for _ in range(3):
            self.assertEqual(self._send().status_code, 200)
        self.assertEqual(len(_rate_counts.get(str(self.tenant.id), [])), 3)

    def test_app_channel_runaway_cron_is_throttled(self):
        # Drive up to the cap, then the next app send must be rejected with 429.
        for _ in range(RATE_LIMIT_PER_HOUR):
            self.assertEqual(self._send().status_code, 200)
        blocked = self._send()
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["error"], "rate_limited")
