"""Tests for cron delivery endpoint.

Post-decommission (Phase 1 — see CONTINUITY_channel_decommission.md) the
resolver is app-or-nothing: a tenant with a registered iOS device routes to the
"app" channel (the delivery IS the APNs push + ?since= feed row written by
record_proactive_outbound — no external chat send), and a tenant with no device
gets 422 no_channel. The Telegram/LINE send helpers remain in the module but are
unreachable from resolve_user_channel.
"""

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.router.cron_delivery import _rate_counts, _split_message
from apps.tenants.models import Tenant
from apps.tenants.test_utils import seed_internal_key


class SplitMessageTest(TestCase):
    def test_short_message(self):
        self.assertEqual(_split_message("hello"), ["hello"])

    def test_long_message_splits_on_paragraph(self):
        text = "A" * 4000 + "\n\n" + "B" * 100
        chunks = _split_message(text, max_len=4096)
        self.assertEqual(len(chunks), 2)

    def test_very_long_message(self):
        text = "A" * 10000
        chunks = _split_message(text, max_len=4096)
        self.assertTrue(all(len(c) <= 4096 for c in chunks))


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    NBHD_DISABLE_BACKGROUND_THREADS=True,
)
class CronDeliveryViewTest(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        from apps.router.models import DeviceToken

        User = get_user_model()
        self.user = User.objects.create_user(username="crontest", password="pass")
        # Telegram is linked AND a device is registered: post-decommission the
        # resolver ignores the Telegram link and routes to the app.
        self.user.telegram_chat_id = 12345
        self.user.save()
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
        )
        DeviceToken.objects.create(tenant=self.tenant, user=self.user, token="d" * 64, environment="production")
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/"
        _rate_counts.clear()

    def _headers(self):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_auth_required(self):
        resp = self.client.post(self.url, {"message": "hello"}, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_missing_message(self):
        resp = self.client.post(self.url, {}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 400)

    def test_successful_send_routes_to_app(self):
        """A device-holding tenant routes to the app: 200, channel="app", and a
        ProactiveOutbound row (which fired the APNs push + ?since= feed row).
        No external chat send is made."""
        resp = self.client.post(self.url, {"message": "Good morning!"}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "sent")
        self.assertEqual(resp.json()["channel"], "app")

        from apps.router.models import ProactiveOutbound

        row = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(row.channel, "app")

    def test_no_device_user_still_routes_to_app(self):
        """A tenant with no registered device (and no Telegram/LINE) still has the
        app/console surface: it routes to "app" (200) and records a
        ProactiveOutbound row rather than 422ing. The APNs push is a best-effort
        no-op when there are zero device tokens — the ?since= feed row IS the
        delivery."""
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.create_user(username="crontest_nodev", password="pass")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
        seed_internal_key(tenant)
        url = f"/api/v1/integrations/runtime/{tenant.id}/send-to-user/"

        resp = self.client.post(
            url,
            {"message": "hello"},
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-key",
            HTTP_X_NBHD_TENANT_ID=str(tenant.id),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["channel"], "app")

        from apps.router.models import ProactiveOutbound

        self.assertTrue(ProactiveOutbound.objects.filter(tenant=tenant, channel="app").exists())

    def test_rate_limit(self):
        import time

        tid = str(self.tenant.id)
        _rate_counts[tid] = [time.time()] * 20  # Fill to limit

        resp = self.client.post(self.url, {"message": "hello"}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 429)

    def test_quick_reply_marker_stripped_before_send_and_persist(self):
        """Proactive/cron sends don't wire up quick-reply buttons (no
        ProactiveOutbound column, no iOS UI for it yet) but the marker must
        still never leak as raw text — it is stripped before the row is
        persisted (the app path stores the same placeholder-space copy)."""
        resp = self.client.post(
            self.url,
            {"message": "Good morning!\n[[quick-replies: Snooze | Done]]"},
            format="json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200)

        from apps.router.models import ProactiveOutbound

        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertNotIn("quick-replies", stored.message_text)
        self.assertIn("Good morning!", stored.message_text)

    def test_journal_link_marker_stripped_from_send_and_persisted(self):
        """The journal deep-link IS persisted for cron/proactive sends: it rides
        ProactiveOutbound.journal_link and surfaces in the iOS ?since= feed as a
        tappable chip. The marker is stripped from the stored message text."""
        resp = self.client.post(
            self.url,
            {"message": "Here's your morning report.\n[[journal-link: daily|2026-07-13|Morning Report]]"},
            format="json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200)

        from apps.router.models import ProactiveOutbound

        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertNotIn("journal-link", stored.message_text)
        self.assertEqual(
            stored.journal_link,
            {"kind": "daily", "slug": "2026-07-13", "title": "Morning Report"},
        )

    def test_no_journal_link_marker_leaves_column_null(self):
        resp = self.client.post(self.url, {"message": "Plain check-in."}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 200)

        from apps.router.models import ProactiveOutbound

        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertIsNone(stored.journal_link)
