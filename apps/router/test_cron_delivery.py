"""Tests for cron delivery endpoint."""

from unittest.mock import MagicMock, patch

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
    TELEGRAM_BOT_TOKEN="test-token",
    NBHD_INTERNAL_API_KEY="test-key",
)
class CronDeliveryViewTest(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(username="crontest", password="pass")
        self.user.telegram_chat_id = 12345
        self.user.save()
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
        )
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

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_successful_send(self, mock_client_cls):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = self.client.post(self.url, {"message": "Good morning!"}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "sent")
        self.assertEqual(resp.json()["chunks"], 1)

    def test_rate_limit(self):
        import time

        tid = str(self.tenant.id)
        _rate_counts[tid] = [time.time()] * 20  # Fill to limit

        resp = self.client.post(self.url, {"message": "hello"}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 429)

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_quick_reply_marker_stripped_before_send_and_persist(self, mock_client_cls):
        """Proactive/cron sends don't wire up quick-reply buttons (no
        ProactiveOutbound column, no iOS UI for it yet) but the marker must
        still never leak as raw text if an agent emits it on a cron message."""
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = self.client.post(
            self.url,
            {"message": "Good morning!\n[[quick-replies: Snooze | Done]]"},
            format="json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200)

        sent_text = mock_http.post.call_args.kwargs["json"]["text"]
        self.assertNotIn("quick-replies", sent_text)
        self.assertIn("Good morning!", sent_text)

        from apps.router.models import ProactiveOutbound

        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertNotIn("quick-replies", stored.message_text)

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_journal_link_marker_stripped_from_send_and_persisted(self, mock_client_cls):
        """Unlike quick-replies, the journal deep-link IS persisted for cron/
        proactive sends: it rides ProactiveOutbound.journal_link and surfaces in
        the iOS ?since= feed, so a Telegram-delivered morning report gets a
        tappable chip in the app. The marker is still stripped from the sent
        Telegram text (no chip transport there)."""
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = self.client.post(
            self.url,
            {"message": "Here's your morning report.\n[[journal-link: daily|2026-07-13|Morning Report]]"},
            format="json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200)

        sent_text = mock_http.post.call_args.kwargs["json"]["text"]
        self.assertNotIn("journal-link", sent_text)
        self.assertIn("morning report", sent_text)

        from apps.router.models import ProactiveOutbound

        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertNotIn("journal-link", stored.message_text)
        self.assertEqual(
            stored.journal_link,
            {"kind": "daily", "slug": "2026-07-13", "title": "Morning Report"},
        )

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_no_journal_link_marker_leaves_column_null(self, mock_client_cls):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = self.client.post(self.url, {"message": "Plain check-in."}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 200)

        from apps.router.models import ProactiveOutbound

        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertIsNone(stored.journal_link)
