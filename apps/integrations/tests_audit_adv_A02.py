"""Adversarial-audit cluster A02 regression tests.

Covers byo_models#2: the RuntimeBYOErrorReportView error path must
reconcile the live container config/env after flipping a BYO credential
to ERROR — otherwise the container keeps its BYO-only openclaw.json (empty
fallback list, ANTHROPIC_API_KEY stripped) and the assistant goes dark for
ALL turns until an unrelated config bump happens to self-heal it.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.test.utils import override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeBYOErrorReconcileTests(TestCase):
    def setUp(self):
        from apps.byo_models.models import BYOCredential

        self.tenant = create_tenant(display_name="BYO Reconcile Tenant", telegram_chat_id=525354)
        seed_internal_key(self.tenant)
        self.cred = BYOCredential.objects.create(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="x",
            status=BYOCredential.Status.VERIFIED,
        )

    def _url(self) -> str:
        return f"/api/v1/internal/runtime/{self.tenant.id}/byo/error/"

    def _headers(self) -> dict[str, str]:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _post_billing_error(self):
        return self.client.post(
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

    def test_error_report_bumps_and_reconciles_container_config(self):
        """The actionable-failure branch must bump pending + regen + apply."""
        before_pending = Tenant.objects.get(id=self.tenant.id).pending_config_version

        with (
            mock.patch("apps.byo_models.services.regenerate_tenant_config") as regen,
            mock.patch("apps.orchestrator.azure_client.apply_byo_credentials_to_container") as apply_byo,
        ):
            response = self._post_billing_error()

        self.assertEqual(response.status_code, 200)

        # Pending config bumped so the apply-pending cron is also a backstop.
        after_pending = Tenant.objects.get(id=self.tenant.id).pending_config_version
        self.assertGreater(after_pending, before_pending)

        # Both reconcile legs fired with the tenant.
        self.assertEqual(regen.call_count, 1)
        self.assertEqual(apply_byo.call_count, 1)
        self.assertEqual(regen.call_args.args[0].id, self.tenant.id)
        self.assertEqual(apply_byo.call_args.args[0].id, self.tenant.id)

    def test_reconcile_failure_still_acks_200(self):
        """A regen/apply failure must be swallowed — the runtime must not retry."""
        with (
            mock.patch(
                "apps.byo_models.services.regenerate_tenant_config",
                side_effect=RuntimeError("boom"),
            ),
            mock.patch(
                "apps.orchestrator.azure_client.apply_byo_credentials_to_container",
                side_effect=RuntimeError("boom"),
            ),
        ):
            response = self._post_billing_error()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_non_actionable_reason_does_not_reconcile(self):
        """Transient reasons (rate-limit/overload) must not touch the container."""
        with (
            mock.patch("apps.byo_models.services.regenerate_tenant_config") as regen,
            mock.patch("apps.orchestrator.azure_client.apply_byo_credentials_to_container") as apply_byo,
        ):
            response = self.client.post(
                self._url(),
                data={
                    "provider": "anthropic",
                    "reason": "rate_limit",
                    "message": "429 slow down",
                    "model_used": "anthropic/claude-sonnet-4-6",
                },
                content_type="application/json",
                **self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(regen.call_count, 0)
        self.assertEqual(apply_byo.call_count, 0)
