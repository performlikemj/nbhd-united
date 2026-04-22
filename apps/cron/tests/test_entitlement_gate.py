"""Tests: cron dispatch must skip tenants without active entitlement."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User


def _make_tenant(*, suffix: int, is_trial=False, trial_ends_at=None, stripe_sub="", **kwargs):
    """Create a tenant with entitlement-relevant fields."""
    user = User.objects.create_user(username=f"ent-user-{suffix}", password="testpass123")
    defaults = {
        "user": user,
        "status": Tenant.Status.ACTIVE,
        "container_id": "oc-test-container",
        "container_fqdn": "oc-test.internal.azurecontainerapps.io",
        "is_trial": is_trial,
        "trial_ends_at": trial_ends_at,
        "stripe_subscription_id": stripe_sub,
        "hibernated_at": None,
    }
    defaults.update(kwargs)
    return Tenant.objects.create(**defaults)


class ApplyPendingConfigsEntitlementTest(TestCase):
    """apply_pending_configs must not seed crons for expired-trial tenants."""

    def setUp(self):
        self.client = APIClient()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.publish.publish_batch", side_effect=lambda tasks: len(tasks))
    def test_skips_expired_trial_tenant(self, mock_batch, mock_verify):
        _make_tenant(
            suffix=1,
            is_trial=True,
            trial_ends_at=timezone.now() - timedelta(hours=2),
            stripe_sub="",
        )

        response = self.client.post("/api/v1/cron/apply-pending-configs/")
        self.assertEqual(response.status_code, 200)

        # Should not include expired trial tenant in cron seeds
        if mock_batch.called:
            batch_tasks = mock_batch.call_args[0][0]
            cron_seeds = [t for t in batch_tasks if t[0] == "seed_cron_jobs"]
            self.assertEqual(len(cron_seeds), 0)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.publish.publish_batch", side_effect=lambda tasks: len(tasks))
    def test_includes_paid_tenant(self, mock_batch, mock_verify):
        _make_tenant(suffix=2, stripe_sub="sub_live_123")

        response = self.client.post("/api/v1/cron/apply-pending-configs/")
        self.assertEqual(response.status_code, 200)

        batch_tasks = mock_batch.call_args[0][0]
        cron_seeds = [t for t in batch_tasks if t[0] == "seed_cron_jobs"]
        self.assertEqual(len(cron_seeds), 1)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.publish.publish_batch", side_effect=lambda tasks: len(tasks))
    def test_includes_valid_trial_tenant(self, mock_batch, mock_verify):
        _make_tenant(
            suffix=3,
            is_trial=True,
            trial_ends_at=timezone.now() + timedelta(days=5),
        )

        response = self.client.post("/api/v1/cron/apply-pending-configs/")
        self.assertEqual(response.status_code, 200)

        batch_tasks = mock_batch.call_args[0][0]
        cron_seeds = [t for t in batch_tasks if t[0] == "seed_cron_jobs"]
        self.assertEqual(len(cron_seeds), 1)


class ForceReseedEntitlementTest(TestCase):
    """force_reseed_crons must skip expired-trial tenants."""

    def setUp(self):
        self.client = APIClient()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_skips_expired_trial(self, mock_gateway, mock_verify):
        _make_tenant(
            suffix=10,
            is_trial=True,
            trial_ends_at=timezone.now() - timedelta(hours=1),
            stripe_sub="",
        )

        response = self.client.post("/api/v1/cron/force-reseed-crons/")
        self.assertEqual(response.status_code, 200)

        # Gateway should never be called for the expired trial tenant
        mock_gateway.assert_not_called()


class BroadcastMessageEntitlementTest(TestCase):
    """broadcast_message must skip expired-trial tenants."""

    def setUp(self):
        self.client = APIClient()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.publish.publish_batch", side_effect=lambda tasks: len(tasks))
    def test_skips_expired_trial(self, mock_batch, mock_verify):
        _make_tenant(
            suffix=20,
            is_trial=True,
            trial_ends_at=timezone.now() - timedelta(hours=1),
            stripe_sub="",
        )

        response = self.client.post(
            "/api/v1/cron/broadcast-message/",
            data={"message": "Hello world"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["enqueued"], 0)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.publish.publish_batch", side_effect=lambda tasks: len(tasks))
    def test_includes_paid_tenant(self, mock_batch, mock_verify):
        _make_tenant(suffix=21, stripe_sub="sub_live_456")

        response = self.client.post(
            "/api/v1/cron/broadcast-message/",
            data={"message": "Hello world"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["enqueued"], 1)


class BroadcastSingleTenantEntitlementTest(TestCase):
    """broadcast_single_tenant_task must skip unentitled tenants."""

    @patch("httpx.post")
    def test_skips_unentitled_tenant(self, mock_post):
        tenant = _make_tenant(
            suffix=30,
            is_trial=True,
            trial_ends_at=timezone.now() - timedelta(hours=1),
            stripe_sub="",
        )

        from apps.orchestrator.tasks import broadcast_single_tenant_task

        broadcast_single_tenant_task(str(tenant.id), "hello")
        mock_post.assert_not_called()

    @patch("httpx.post")
    def test_sends_to_entitled_tenant(self, mock_post):
        from unittest.mock import MagicMock

        mock_post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        tenant = _make_tenant(suffix=31, stripe_sub="sub_live_789")

        from apps.orchestrator.tasks import broadcast_single_tenant_task

        broadcast_single_tenant_task(str(tenant.id), "hello")
        mock_post.assert_called_once()
