"""Tests for cron delivery endpoint."""

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.router.cron_delivery import _rate_counts, _split_message
from apps.tenants.models import Tenant


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
