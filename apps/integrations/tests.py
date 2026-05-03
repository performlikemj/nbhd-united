"""Tests for internal runtime usage reporting."""

from __future__ import annotations

from django.test import TestCase
from django.test.utils import override_settings

from apps.billing.constants import MINIMAX_MODEL
from apps.billing.models import UsageRecord
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeUsageReportTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Usage Tenant", telegram_chat_id=424242)

    def _url(self, tenant_id: str | None = None) -> str:
        return f"/api/v1/internal/runtime/{tenant_id or self.tenant.id}/usage/report/"

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
                "model_used": MINIMAX_MODEL,
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
        self.assertEqual(record.model_used, MINIMAX_MODEL)

    def test_missing_auth_returns_401(self):
        response = self.client.post(
            self._url(),
            data={
                "event_type": "message",
                "input_tokens": 10,
                "output_tokens": 5,
                "model_used": MINIMAX_MODEL,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "internal_auth_failed")

    def test_invalid_payload_returns_400(self):
        response = self.client.post(
            self._url(),
            data={
                "event_type": "message",
                "input_tokens": -12,
                "output_tokens": 10,
                "model_used": MINIMAX_MODEL,
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_request")

    def test_missing_fields_returns_400(self):
        response = self.client.post(
            self._url(),
            data={
                "event_type": "message",
                "output_tokens": 10,
                "model_used": MINIMAX_MODEL,
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
                "model_used": MINIMAX_MODEL,
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)

        tenant = Tenant.objects.get(id=self.tenant.id)
        self.assertEqual(tenant.messages_today, before_messages_today + 1)
        self.assertEqual(tenant.messages_this_month, before_messages_month + 1)
        self.assertEqual(tenant.tokens_this_month, before_tokens + 150)
        self.assertGreater(tenant.estimated_cost_this_month, before.estimated_cost_this_month)


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeBYOErrorReportTests(TestCase):
    """Tests for the runtime BYO error reporting endpoint.

    Closes the loop opened by `fix(byo): no-silent-fallback` — when
    OpenClaw raises a billing/auth error on a BYO route, the in-container
    `nbhd-usage-reporter` plugin POSTs here so the AI Provider page can
    surface the real cause to the user.
    """

    def setUp(self):
        from apps.byo_models.models import BYOCredential

        self.tenant = create_tenant(display_name="BYO Err Tenant", telegram_chat_id=424243)
        self.cred = BYOCredential.objects.create(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="x",
            status=BYOCredential.Status.VERIFIED,
        )

    def _url(self, tenant_id: str | None = None) -> str:
        return f"/api/v1/internal/runtime/{tenant_id or self.tenant.id}/byo/error/"

    def _headers(self, tenant_id: str | None = None, key: str = "shared-key") -> dict[str, str]:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def test_billing_error_flips_credential_to_error_status(self):
        from apps.byo_models.models import BYOCredential

        response = self.client.post(
            self._url(),
            data={
                "provider": "anthropic",
                "reason": "billing",
                "message": "You're out of extra usage",
                "model_used": "anthropic/claude-sonnet-4-6",
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.cred.refresh_from_db()
        self.assertEqual(self.cred.status, BYOCredential.Status.ERROR)
        self.assertIn("Claude account", self.cred.last_error)
        self.assertIn("usage", self.cred.last_error)

    def test_auth_error_flips_credential_with_reconnect_hint(self):
        from apps.byo_models.models import BYOCredential

        response = self.client.post(
            self._url(),
            data={
                "provider": "anthropic",
                "reason": "auth",
                "message": "401 Unauthorized: token expired",
                "model_used": "anthropic/claude-sonnet-4-6",
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.cred.refresh_from_db()
        self.assertEqual(self.cred.status, BYOCredential.Status.ERROR)
        self.assertIn("Reconnect", self.cred.last_error)

    def test_non_actionable_reason_is_ignored(self):
        from apps.byo_models.models import BYOCredential

        response = self.client.post(
            self._url(),
            data={
                "provider": "anthropic",
                "reason": "rate_limit",
                "message": "429 Too Many Requests",
                "model_used": "anthropic/claude-sonnet-4-6",
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")
        self.cred.refresh_from_db()
        self.assertEqual(self.cred.status, BYOCredential.Status.VERIFIED)

    def test_unknown_provider_returns_400(self):
        response = self.client.post(
            self._url(),
            data={
                "provider": "bogus",
                "reason": "billing",
                "message": "no money",
                "model_used": "x",
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400)

    def test_missing_auth_returns_401(self):
        response = self.client.post(
            self._url(),
            data={
                "provider": "anthropic",
                "reason": "billing",
                "message": "no money",
                "model_used": "anthropic/claude-sonnet-4-6",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)

    def test_no_credential_returns_200_with_no_credential_status(self):
        # Disconnected credential — runtime might still be mid-flight.
        self.cred.delete()

        response = self.client.post(
            self._url(),
            data={
                "provider": "anthropic",
                "reason": "billing",
                "message": "no money",
                "model_used": "anthropic/claude-sonnet-4-6",
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_credential")
