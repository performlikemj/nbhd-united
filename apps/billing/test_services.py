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
    @patch("apps.orchestrator.azure_client.scale_container_app")
    @patch("apps.cron.publish.publish_task")
    def test_reactivation_enqueues_resume_crons_with_delay(
        self,
        mock_publish,
        mock_scale,
        mock_payload_sync,
    ):
        """Suspended→Active transition must enqueue ``resume_tenant_crons``
        with a ~30s delay, NOT call it synchronously. The container was
        hibernated and is just starting to wake — its gateway is not
        listening yet, so a synchronous ``cron.list``/``cron.update``
        round-trip would 502 inside the webhook handler. See issue #540.

        The delayed enqueue is what re-enables the customer's morning
        briefing etc. after a real resub; the explicit assertion here
        pins the contract that no synchronous gateway-touching cron op
        runs from the webhook handler.
        """
        self.tenant.is_trial = False
        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.container_id = "oc-reactivate"
        self.tenant.container_fqdn = "oc-reactivate.internal.example.io"
        self.tenant.save(
            update_fields=["is_trial", "status", "container_id", "container_fqdn", "updated_at"],
        )
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

        resume_calls = [
            call for call in mock_publish.call_args_list if call.args and call.args[0] == "resume_tenant_crons"
        ]
        self.assertEqual(
            len(resume_calls), 1, f"expected one resume_tenant_crons publish, got {mock_publish.call_args_list}"
        )
        call = resume_calls[0]
        self.assertEqual(call.args[1], str(self.tenant.id))
        self.assertEqual(call.kwargs.get("idempotency_key"), f"resume-crons-{self.tenant.id}")
        # Delay must be long enough for the container's gateway to boot.
        # 30s is the current value; flag if anyone shortens it without
        # also revisiting the cold-start window in #540.
        self.assertGreaterEqual(call.kwargs.get("delay_seconds", 0), 30)

        # Refresh still happens (Postgres-only, no cold-start race).
        mock_payload_sync.assert_called_once()

    @patch("apps.orchestrator.services.refresh_system_cron_rows_from_seed")
    @patch("apps.orchestrator.azure_client.scale_container_app")
    @patch("apps.cron.publish.publish_task")
    def test_reactivation_continues_when_cron_payload_sync_fails(
        self,
        mock_publish,
        mock_scale,
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

    @patch("apps.orchestrator.services.refresh_system_cron_rows_from_seed")
    @patch("apps.orchestrator.azure_client.scale_container_app")
    @patch("apps.cron.publish.publish_task")
    def test_reactivation_swallows_resume_enqueue_failure(
        self,
        mock_publish,
        mock_scale,
        mock_payload_sync,
    ):
        """If QStash is unreachable, the failure to enqueue
        ``resume_tenant_crons`` must NOT crash reactivation. The hourly
        ``reconcile_tenant_crons`` sweep eventually fixes drift on the
        ``enabled`` field via the cron-drift detector.
        """
        self.tenant.is_trial = False
        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.container_id = "oc-reactivate-qstash-down"
        self.tenant.container_fqdn = "oc-reactivate-qstash-down.internal.example.io"
        self.tenant.save(
            update_fields=["is_trial", "status", "container_id", "container_fqdn", "updated_at"],
        )
        mock_payload_sync.return_value = {"created": 0, "updated": 0, "preserved_custom": 0, "unchanged": 9}

        def selective_fail(task_name, *args, **kwargs):
            if task_name == "resume_tenant_crons":
                raise RuntimeError("qstash down")

        mock_publish.side_effect = selective_fail

        # Must not raise.
        handle_checkout_completed(
            {
                "metadata": {"user_id": str(self.tenant.user_id), "tier": "starter"},
                "customer": "cus_react_qstash",
                "subscription": "sub_react_qstash",
            }
        )

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)

    @patch("apps.cron.publish.publish_task")
    def test_subscription_deleted_finds_tenant_by_subscription_id(self, mock_publish):
        self.tenant.stripe_subscription_id = "sub_lookup"
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["stripe_subscription_id", "status", "updated_at"])

        handle_subscription_deleted({"id": "sub_lookup"})

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.DEPROVISIONING)
        mock_publish.assert_called_once_with("deprovision_tenant", str(self.tenant.id))

    @patch("apps.cron.publish.publish_task")
    def test_subscription_deleted_skips_budget_exempt_tenants(self, mock_publish):
        """Infrastructure-class tenants (canary, internal accounts) carry
        ``is_budget_exempt=True``. A Stripe subscription cancel event for
        these tenants must NOT trigger deprovision — they exist outside
        the normal billing lifecycle and their test-mode Stripe state can
        cycle through cancels without any signal the tenant should be
        torn down. Without this guard, today's canary incident recurs:
        a test event fires, deprovision_tenant publishes, the container
        delete fails on the RG lock, status flips to SUSPENDED, repeat.
        """
        self.tenant.stripe_subscription_id = "sub_canary"
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.is_budget_exempt = True
        self.tenant.save(
            update_fields=[
                "stripe_subscription_id",
                "status",
                "is_budget_exempt",
                "updated_at",
            ]
        )

        handle_subscription_deleted({"id": "sub_canary"})

        self.tenant.refresh_from_db()
        # Status unchanged — no deprovision triggered.
        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.tenants.views._do_hard_delete")
    def test_pending_deletion_overrides_budget_exempt(self, mock_hard_delete, mock_publish):
        """``pending_deletion`` is explicit user intent and takes priority
        over the exempt guard — an exempt tenant who has requested hard
        delete still gets deleted when their subscription ends.
        """
        self.tenant.stripe_subscription_id = "sub_canary_delete"
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.is_budget_exempt = True
        self.tenant.pending_deletion = True
        self.tenant.save(
            update_fields=[
                "stripe_subscription_id",
                "status",
                "is_budget_exempt",
                "pending_deletion",
                "updated_at",
            ]
        )

        handle_subscription_deleted({"id": "sub_canary_delete"})

        mock_hard_delete.assert_called_once_with(self.tenant.user)
        # Deprovision queue path is bypassed in favor of hard delete.
        mock_publish.assert_not_called()

    def test_invoice_payment_failed_suspends_tenant_by_customer(self):
        self.tenant.stripe_customer_id = "cus_lookup"
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["stripe_customer_id", "status", "updated_at"])

        handle_invoice_payment_failed({"customer": "cus_lookup"})

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.SUSPENDED)
