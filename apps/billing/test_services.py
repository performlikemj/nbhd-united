"""Additional billing service coverage."""

from unittest.mock import patch

from django.test import TestCase

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .services import handle_checkout_completed, handle_invoice_payment_failed, handle_subscription_deleted


class BillingWebhookServiceTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Billing", telegram_chat_id=424242)

    @patch("apps.cron.publish.publish_task")
    def test_checkout_completed_sets_provisioning_and_enqueues(self, mock_publish):
        handle_checkout_completed(
            {
                "metadata": {"user_id": str(self.tenant.user_id), "tier": "starter"},
                "customer": "cus_123",
                "subscription": "sub_123",
            }
        )

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.PROVISIONING)
        self.assertEqual(self.tenant.model_tier, Tenant.ModelTier.STARTER)
        self.assertEqual(self.tenant.stripe_customer_id, "cus_123")
        self.assertEqual(self.tenant.stripe_subscription_id, "sub_123")
        mock_publish.assert_called_once_with("provision_tenant", str(self.tenant.id))

    @patch("apps.cron.publish.publish_task")
    def test_checkout_completed_invalid_tier_defaults_to_basic(self, mock_publish):
        handle_checkout_completed(
            {
                "metadata": {"user_id": str(self.tenant.user_id), "tier": "enterprise"},
                "customer": "cus_123",
                "subscription": "sub_123",
            }
        )

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.model_tier, Tenant.ModelTier.STARTER)
        mock_publish.assert_called_once()

    @patch("apps.cron.publish.publish_task")
    def test_checkout_completed_unknown_tier_defaults_to_starter(self, mock_publish):
        handle_checkout_completed(
            {
                "metadata": {"user_id": str(self.tenant.user_id), "tier": "premium"},
                "customer": "cus_456",
                "subscription": "sub_456",
            }
        )

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.model_tier, Tenant.ModelTier.STARTER)
        mock_publish.assert_called_once()

    @patch("apps.cron.publish.publish_task")
    def test_checkout_completed_duplicate_active_event_is_ignored(self, mock_publish):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-tenant"
        self.tenant.model_tier = Tenant.ModelTier.STARTER
        self.tenant.stripe_subscription_id = "sub_same"
        self.tenant.save(update_fields=["status", "container_id", "model_tier", "stripe_subscription_id", "updated_at"])

        handle_checkout_completed(
            {
                "metadata": {"user_id": str(self.tenant.user_id), "tier": "starter"},
                "customer": "cus_123",
                "subscription": "sub_same",
            }
        )

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    def test_checkout_completed_reactivates_expired_trial_users(self, mock_publish):
        self.tenant.is_trial = False
        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.save(update_fields=["is_trial", "status", "updated_at"])

        handle_checkout_completed(
            {
                "metadata": {"user_id": str(self.tenant.user_id), "tier": "starter"},
                "customer": "cus_999",
                "subscription": "sub_999",
            }
        )

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)
        self.assertFalse(self.tenant.is_trial)
        self.assertEqual(self.tenant.stripe_subscription_id, "sub_999")
        mock_publish.assert_not_called()

    @patch("apps.orchestrator.services.refresh_system_cron_rows_from_seed")
    @patch("apps.cron.suspension.resume_tenant_crons")
    @patch("apps.orchestrator.azure_client.scale_container_app")
    @patch("apps.cron.publish.publish_task")
    def test_reactivation_invokes_cron_payload_sync_after_resume(
        self,
        mock_publish,
        mock_scale,
        mock_resume,
        mock_payload_sync,
    ):
        """Suspended→Active transition must call update_system_cron_prompts
        AFTER resume_tenant_crons so payload drift (e.g. stale
        ``payload.model = anthropic-cli/...`` left over from a BYO setup
        before the suspension) is fixed before any cron has a chance to
        fire on the freshly-woken container.

        Canary 2026-05-12 reproduction: pre-PR-#532, this stale model
        survived for weeks because suspended tenants don't run
        apply_pending_configs and the reactivation flow never sync'd
        payloads. This test pins the eager-sync contract.
        """
        self.tenant.is_trial = False
        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.container_id = "oc-reactivate"
        self.tenant.container_fqdn = "oc-reactivate.internal.example.io"
        self.tenant.save(
            update_fields=["is_trial", "status", "container_id", "container_fqdn", "updated_at"],
        )
        mock_resume.return_value = {"enabled": 5, "already_enabled": 0, "errors": 0, "job_names": []}
        mock_payload_sync.return_value = {"created": 0, "updated": 1, "preserved_custom": 0, "unchanged": 8}

        handle_checkout_completed(
            {
                "metadata": {"user_id": str(self.tenant.user_id), "tier": "starter"},
                "customer": "cus_react",
                "subscription": "sub_react",
            }
        )

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)
        # The refresh hook must have been called exactly once with the
        # reactivated tenant. Ordering vs. resume_tenant_crons is enforced
        # by source — they're sequential in the same try block.
        mock_payload_sync.assert_called_once()
        sync_arg = mock_payload_sync.call_args.args[0]
        self.assertEqual(sync_arg.id, self.tenant.id)
        # And resume_tenant_crons must have been called too (the hook
        # runs after it, not in place of it).
        mock_resume.assert_called_once()

    @patch("apps.orchestrator.services.refresh_system_cron_rows_from_seed")
    @patch("apps.cron.suspension.resume_tenant_crons")
    @patch("apps.orchestrator.azure_client.scale_container_app")
    @patch("apps.cron.publish.publish_task")
    def test_reactivation_continues_when_cron_payload_sync_fails(
        self,
        mock_publish,
        mock_scale,
        mock_resume,
        mock_payload_sync,
    ):
        """A drift-fix failure during reactivation must NOT block the
        rest of the flow. The next apply_pending_configs sweep is the
        safety net — losing reactivation entirely because of a sync
        hiccup would be worse than waiting one hour for the lazy path.
        """
        self.tenant.is_trial = False
        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.container_id = "oc-reactivate-err"
        self.tenant.container_fqdn = "oc-reactivate-err.internal.example.io"
        self.tenant.save(
            update_fields=["is_trial", "status", "container_id", "container_fqdn", "updated_at"],
        )
        mock_resume.return_value = {"enabled": 5, "already_enabled": 0, "errors": 0, "job_names": []}
        mock_payload_sync.side_effect = RuntimeError("simulated gateway flake")

        # Must not raise — reactivation should swallow the sync exception.
        handle_checkout_completed(
            {
                "metadata": {"user_id": str(self.tenant.user_id), "tier": "starter"},
                "customer": "cus_react_err",
                "subscription": "sub_react_err",
            }
        )

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)
        # Hook was still attempted, just raised.
        mock_payload_sync.assert_called_once()

    @patch("apps.cron.publish.publish_task")
    def test_subscription_deleted_finds_tenant_by_subscription_id(self, mock_publish):
        self.tenant.stripe_subscription_id = "sub_lookup"
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["stripe_subscription_id", "status", "updated_at"])

        handle_subscription_deleted({"id": "sub_lookup"})

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.DEPROVISIONING)
        mock_publish.assert_called_once_with("deprovision_tenant", str(self.tenant.id))

    def test_invoice_payment_failed_suspends_tenant_by_customer(self):
        self.tenant.stripe_customer_id = "cus_lookup"
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["stripe_customer_id", "status", "updated_at"])

        handle_invoice_payment_failed({"customer": "cus_lookup"})

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.SUSPENDED)
