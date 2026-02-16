"""Additional billing service coverage."""
from unittest.mock import patch

from django.test import TestCase

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .services import (
    handle_checkout_completed,
    handle_invoice_payment_failed,
    handle_subscription_deleted,
)


class BillingWebhookServiceTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Billing", telegram_chat_id=424242)

    @patch("apps.cron.publish.publish_task")
    def test_checkout_completed_sets_provisioning_and_enqueues(self, mock_publish):
        handle_checkout_completed(
            {
                "metadata": {"user_id": str(self.tenant.user_id), "tier": "premium"},
                "customer": "cus_123",
                "subscription": "sub_123",
            }
        )

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.PROVISIONING)
        self.assertEqual(self.tenant.model_tier, Tenant.ModelTier.PREMIUM)
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
    def test_checkout_completed_duplicate_active_event_is_ignored(self, mock_publish):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-tenant"
        self.tenant.model_tier = Tenant.ModelTier.STARTER
        self.tenant.stripe_subscription_id = "sub_same"
        self.tenant.save(
            update_fields=["status", "container_id", "model_tier", "stripe_subscription_id", "updated_at"]
        )

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
