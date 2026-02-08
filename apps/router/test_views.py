"""Router webhook view tests."""
import json
from unittest.mock import AsyncMock, patch

from django.test import TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from apps.router.services import clear_cache, clear_rate_limits


@override_settings(TELEGRAM_WEBHOOK_SECRET="test-secret", ROUTER_RATE_LIMIT_PER_MINUTE=10)
class TelegramWebhookViewTest(TestCase):
    def setUp(self):
        clear_cache()
        clear_rate_limits()

    def tearDown(self):
        clear_cache()
        clear_rate_limits()

    def _post_update(self, payload: dict):
        return self.client.post(
            "/api/v1/telegram/webhook/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-secret",
        )

    def test_unknown_chat_returns_onboarding_payload(self):
        response = self._post_update({"message": {"chat": {"id": 999000111}}})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["method"], "sendMessage")
        self.assertEqual(body["chat_id"], 999000111)

    def test_inactive_tenant_gets_onboarding_payload(self):
        tenant = create_tenant(display_name="Inactive", telegram_chat_id=777111)
        tenant.status = Tenant.Status.SUSPENDED
        tenant.container_fqdn = "oc-inactive.internal.azurecontainerapps.io"
        tenant.save(update_fields=["status", "container_fqdn", "updated_at"])

        response = self._post_update({"message": {"chat": {"id": 777111}}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["method"], "sendMessage")

    @patch("apps.router.views.forward_to_openclaw", new_callable=AsyncMock)
    def test_active_tenant_update_is_forwarded(self, mock_forward):
        tenant = create_tenant(display_name="Active", telegram_chat_id=123456)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_fqdn = "oc-active.internal.azurecontainerapps.io"
        tenant.save(update_fields=["status", "container_fqdn", "updated_at"])
        mock_forward.return_value = {"ok": True}

        response = self._post_update({"message": {"chat": {"id": 123456}}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        mock_forward.assert_awaited_once()


@override_settings(TELEGRAM_WEBHOOK_SECRET="test-secret", ROUTER_RATE_LIMIT_PER_MINUTE=1)
class TelegramWebhookRateLimitTest(TestCase):
    def setUp(self):
        clear_cache()
        clear_rate_limits()

    def tearDown(self):
        clear_cache()
        clear_rate_limits()

    def _post_update(self, chat_id: int):
        return self.client.post(
            "/api/v1/telegram/webhook/",
            data=json.dumps({"message": {"chat": {"id": chat_id}}}),
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-secret",
        )

    def test_second_request_within_window_is_rate_limited(self):
        first = self._post_update(4242)
        second = self._post_update(4242)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
