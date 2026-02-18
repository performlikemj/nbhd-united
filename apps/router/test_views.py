"""Router webhook view tests."""
import json
from unittest.mock import AsyncMock, patch

from django.utils import timezone

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

    def _post_update(self, payload: dict, secret: str = "test-secret"):
        extra_headers = {}
        if secret is not None:
            extra_headers["HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"] = secret

        return self.client.post(
            "/api/v1/telegram/webhook/",
            data=json.dumps(payload),
            content_type="application/json",
            **extra_headers,
        )

    def test_unknown_chat_returns_onboarding_payload(self):
        response = self._post_update({"message": {"chat": {"id": 999000111}}})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["method"], "sendMessage")
        self.assertEqual(body["chat_id"], 999000111)
        self.assertIn("Sign up at", body["text"])

    @override_settings(FRONTEND_URL="https://console.example.com")
    def test_unknown_chat_message_uses_frontend_url_setting(self):
        response = self._post_update({"message": {"chat": {"id": 101010}}})
        body = response.json()
        self.assertIn("https://console.example.com", body["text"])

    def test_invalid_secret_returns_403(self):
        response = self._post_update({"message": {"chat": {"id": 1}}}, secret="wrong-secret")
        self.assertEqual(response.status_code, 403)

    def test_inactive_tenant_gets_onboarding_payload(self):
        tenant = create_tenant(display_name="Inactive", telegram_chat_id=777111)
        tenant.status = Tenant.Status.SUSPENDED
        tenant.container_fqdn = "oc-inactive.internal.azurecontainerapps.io"
        tenant.save(update_fields=["status", "container_fqdn", "updated_at"])

        response = self._post_update({"message": {"chat": {"id": 777111}}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["method"], "sendMessage")

    def test_suspended_trial_tenant_is_prompted_to_subscribe(self):
        tenant = create_tenant(display_name="Trial Expired", telegram_chat_id=777222)
        tenant.status = Tenant.Status.SUSPENDED
        tenant.is_trial = False
        tenant.container_fqdn = "oc-inactive.internal.azurecontainerapps.io"
        tenant.save(update_fields=["status", "is_trial", "container_fqdn", "updated_at"] )

        response = self._post_update({"message": {"chat": {"id": 777222}}})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["method"], "sendMessage")
        self.assertIn("Your free trial has ended", body["text"])
        self.assertIn("/settings/billing", body["text"])

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
        self.assertEqual(mock_forward.await_args.kwargs.get("user_timezone"), "UTC")
        self.assertEqual(mock_forward.await_args.kwargs.get("timeout"), 30.0)
        self.assertEqual(mock_forward.await_args.kwargs.get("max_retries"), 1)
        self.assertEqual(mock_forward.await_args.kwargs.get("retry_delay"), 5.0)

    @patch("apps.router.views.forward_to_openclaw", new_callable=AsyncMock)
    def test_active_tenant_updates_last_message_at(self, mock_forward):
        tenant = create_tenant(display_name="ActiveLastMessage", telegram_chat_id=123450)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_fqdn = "oc-active.internal.azurecontainerapps.io"
        tenant.last_message_at = None
        tenant.save(update_fields=["status", "container_fqdn", "last_message_at", "updated_at"])
        mock_forward.return_value = {"ok": True}

        response = self._post_update({"message": {"chat": {"id": 123450}}})

        self.assertEqual(response.status_code, 200)
        tenant.refresh_from_db()
        self.assertIsNotNone(tenant.last_message_at)
        self.assertLessEqual((timezone.now() - tenant.last_message_at).total_seconds(), 5)

    @patch("apps.router.views.record_usage")
    @patch("apps.router.views.forward_to_openclaw", new_callable=AsyncMock)
    def test_active_tenant_records_usage_on_forward_success(self, mock_forward, mock_record_usage):
        tenant = create_tenant(display_name="Active", telegram_chat_id=123457)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_fqdn = "oc-active.internal.azurecontainerapps.io"
        tenant.save(update_fields=["status", "container_fqdn", "updated_at"])

        mock_forward.return_value = {
            "ok": True,
            "usage": {
                "input_tokens": 12,
                "output_tokens": 34,
                "model_used": "anthropic/claude-sonnet-4-20250514",
            },
        }

        response = self._post_update({"message": {"chat": {"id": 123457}}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        self.assertEqual(mock_record_usage.call_count, 1)
        mock_record_usage.assert_called_once_with(
            tenant=tenant,
            event_type="message",
            input_tokens=12,
            output_tokens=34,
            model_used="anthropic/claude-sonnet-4-20250514",
        )

    @patch("apps.router.views.forward_to_openclaw", new_callable=AsyncMock)
    def test_active_tenant_over_budget_is_blocked(self, mock_forward):
        tenant = create_tenant(display_name="QuotaBlocked", telegram_chat_id=123458)
        tenant.status = Tenant.Status.ACTIVE
        tenant.monthly_token_budget = 500
        tenant.tokens_this_month = 500
        tenant.container_fqdn = "oc-active.internal.azurecontainerapps.io"
        tenant.save(
            update_fields=[
                "status", "monthly_token_budget", "tokens_this_month", "container_fqdn",
                "updated_at",
            ]
        )

        response = self._post_update({"message": {"chat": {"id": 123458}}})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["method"], "sendMessage")
        self.assertIn("quota", body["text"].lower())
        self.assertIn("/billing", body["text"])
        mock_forward.assert_not_called()

    @patch("apps.router.views.forward_to_openclaw", new_callable=AsyncMock)
    def test_active_tenant_forwards_user_timezone(self, mock_forward):
        tenant = create_tenant(display_name="TZ", telegram_chat_id=123789)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_fqdn = "oc-active.internal.azurecontainerapps.io"
        tenant.user.timezone = "Asia/Tokyo"
        tenant.user.save(update_fields=["timezone"])
        tenant.save(update_fields=["status", "container_fqdn", "updated_at"])
        mock_forward.return_value = {"ok": True}

        response = self._post_update({"message": {"chat": {"id": 123789}}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_forward.await_args.kwargs.get("user_timezone"), "Asia/Tokyo")

    @patch("apps.router.views.forward_to_openclaw", new_callable=AsyncMock)
    def test_forwarding_failure_sends_retry_message(self, mock_forward):
        """When forwarding returns None, user gets a friendly retry message."""
        tenant = create_tenant(display_name="Failing", telegram_chat_id=987654)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_fqdn = "oc-failing.internal.azurecontainerapps.io"
        tenant.save(update_fields=["status", "container_fqdn", "updated_at"])
        mock_forward.return_value = None

        response = self._post_update({"message": {"chat": {"id": 987654}}})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["method"], "sendMessage")
        self.assertEqual(body["chat_id"], 987654)
        self.assertIn("30 seconds", body["text"])


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


@override_settings(TELEGRAM_WEBHOOK_SECRET="")
class TelegramWebhookSecretConfigTest(TestCase):
    def test_missing_config_returns_503_even_with_no_header(self):
        response = self.client.post(
            "/api/v1/telegram/webhook/",
            data=json.dumps({"message": {"chat": {"id": 1}}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 503)
