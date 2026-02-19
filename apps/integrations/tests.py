"""Tests for internal runtime usage reporting."""
from __future__ import annotations

from django.test import TestCase
from django.test.utils import override_settings

from apps.billing.models import UsageRecord
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeUsageReportTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Usage Tenant", telegram_chat_id=424242)

    def _url(self) -> str:
        return "/api/v1/internal/runtime/usage/report/"

    def _headers(self, tenant_id: str | None = None, key: str = "shared-key") -> dict[str, str]:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def test_valid_usage_report_creates_usage_record(self):
        response = self.client.post(
            self._url(),
            data={
                "event_type": "message",
                "input_tokens": 1234,
                "output_tokens": 567,
                "model_used": "openrouter/moonshotai/kimi-k2.5",
                "timestamp": "2026-02-20T01:00:00Z",
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        record = UsageRecord.objects.get()
        self.assertEqual(record.tenant_id, self.tenant.id)
        self.assertEqual(record.event_type, "message")
        self.assertEqual(record.input_tokens, 1234)
        self.assertEqual(record.output_tokens, 567)
        self.assertEqual(record.model_used, "openrouter/moonshotai/kimi-k2.5")

    def test_missing_auth_returns_403(self):
        response = self.client.post(
            self._url(),
            data={
                "event_type": "message",
                "input_tokens": 10,
                "output_tokens": 5,
                "model_used": "openrouter/moonshotai/kimi-k2.5",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "internal_auth_failed")

    def test_invalid_payload_returns_400(self):
        response = self.client.post(
            self._url(),
            data={
                "event_type": "message",
                "input_tokens": -12,
                "output_tokens": 10,
                "model_used": "openrouter/moonshotai/kimi-k2.5",
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_request")

    def test_usage_updates_tenant_counters(self):
        before = Tenant.objects.get(id=self.tenant.id)
        before_tokens = before.tokens_this_month
        before_messages_today = before.messages_today
        before_messages_month = before.messages_this_month

        response = self.client.post(
            self._url(),
            data={
                "event_type": "message",
                "input_tokens": 100,
                "output_tokens": 50,
                "model_used": "openrouter/moonshotai/kimi-k2.5",
                "timestamp": "2026-02-20T01:00:00Z",
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)

        tenant = Tenant.objects.get(id=self.tenant.id)
        self.assertEqual(tenant.messages_today, before_messages_today + 1)
        self.assertEqual(tenant.messages_this_month, before_messages_month + 1)
        self.assertEqual(tenant.tokens_this_month, before_tokens + 150)
        self.assertIsNotNone(tenant.last_message_at)
        self.assertEqual(tenant.last_message_at.isoformat(), "2026-02-20T01:00:00+00:00")

        self.assertGreater(tenant.estimated_cost_this_month, before.estimated_cost_this_month)
