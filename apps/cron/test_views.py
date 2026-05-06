"""Tests for QStash cron trigger endpoints."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User


def _create_tenant_with_config_state(
    *,
    active: bool = True,
    config_version: int = 0,
    pending_config_version: int = 0,
    last_message_at=None,
    has_container: bool = True,
    suffix: int = 0,
):
    user = User.objects.create_user(
        username=f"user-{pending_config_version}-{config_version}-{suffix}", password="testpass123"
    )
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE if active else Tenant.Status.PENDING,
        model_tier=Tenant.ModelTier.STARTER,
        container_id="oc-test" if has_container else "",
        container_fqdn="oc-test.internal.azurecontainerapps.io" if has_container else "",
        config_version=config_version,
        pending_config_version=pending_config_version,
        last_message_at=last_message_at,
    )
    return tenant


class ApplyPendingConfigsTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.publish.publish_batch", side_effect=lambda tasks: len(tasks))
    def test_apply_pending_configs_enqueues_idle_tenants_only(self, mock_batch, mock_verify):
        now = timezone.now()
        ready = _create_tenant_with_config_state(
            pending_config_version=2,
            config_version=1,
            last_message_at=None,
            suffix=1,
        )
        stale = _create_tenant_with_config_state(
            pending_config_version=2,
            config_version=1,
            last_message_at=now - timedelta(minutes=16),
            has_container=True,
            suffix=2,
        )
        active_recent = _create_tenant_with_config_state(
            pending_config_version=2,
            config_version=1,
            last_message_at=now - timedelta(minutes=5),
            suffix=3,
        )
        updated_pending = _create_tenant_with_config_state(
            pending_config_version=1,
            config_version=1,
            last_message_at=now - timedelta(minutes=40),
            suffix=4,
        )
        inactive = _create_tenant_with_config_state(
            active=False,
            pending_config_version=2,
            config_version=0,
            last_message_at=None,
            suffix=5,
        )

        response = self.client.post("/api/v1/cron/apply-pending-configs/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["config_enqueued"], 2)
        self.assertEqual(body["config_failed"], 0)
        self.assertEqual(body["evaluated"], 2)

        # Verify publish_batch was called with tasks for each eligible tenant
        batch_tasks = mock_batch.call_args[0][0]
        config_calls = [t for t in batch_tasks if t[0] == "apply_single_tenant_config"]
        self.assertEqual(len(config_calls), 2)

        # Config version NOT bumped yet — that happens in the async task
        ready.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(ready.config_version, 1)
        self.assertEqual(stale.config_version, 1)


class CronAuthTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_apply_pending_configs_rejects_invalid_signature(self):
        response = self.client.post("/api/v1/cron/apply-pending-configs/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Invalid signature")


class ExpireTrialsCronTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        user = User.objects.create_user(username="trial-expired-owner", password="testpass123")
        self.user = user

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_expire_trials_suspends_unpaid_expired_trials(self, mock_verify):
        expired_trial = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            is_trial=True,
            trial_started_at=timezone.now() - timedelta(days=8),
            trial_ends_at=timezone.now() - timedelta(hours=1),
            stripe_subscription_id="",
        )

        active_premium = Tenant.objects.create(
            user=User.objects.create_user(username="trial-paid-owner", password="testpass123"),
            status=Tenant.Status.ACTIVE,
            is_trial=True,
            trial_started_at=timezone.now() - timedelta(days=8),
            trial_ends_at=timezone.now() - timedelta(hours=1),
            stripe_subscription_id="sub_123",
        )

        response = self.client.post("/api/v1/cron/expire-trials/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated"], 1)

        expired_trial.refresh_from_db()
        active_premium.refresh_from_db()

        self.assertFalse(expired_trial.is_trial)
        self.assertEqual(expired_trial.status, Tenant.Status.SUSPENDED)
        self.assertEqual(active_premium.status, Tenant.Status.ACTIVE)
        self.assertTrue(active_premium.is_trial)
        self.assertEqual(active_premium.stripe_subscription_id, "sub_123")

    @patch("apps.cron.views.verify_qstash_signature", return_value=False)
    def test_expire_trials_rejects_invalid_or_missing_signature(self, mock_verify):
        response = self.client.post("/api/v1/cron/expire-trials/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Invalid signature")


class RestartTenantContainerTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.views.restart_container_app")
    def test_restart_tenant_container_calls_restart(self, mock_restart, mock_verify):
        user = User.objects.create_user(username="tenant-restart", password="testpass123")
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            model_tier=Tenant.ModelTier.STARTER,
            container_id="oc-restart-test",
            container_fqdn="oc-restart.internal.azurecontainerapps.io",
        )

        response = self.client.post(
            "/api/v1/cron/restart-tenant-container/",
            data=json.dumps({"tenant_id": str(tenant.id)}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["restarted"], True)
        self.assertEqual(response.json()["container"], "oc-restart-test")
        mock_restart.assert_called_once_with("oc-restart-test")

    @patch("apps.cron.views.verify_qstash_signature", return_value=False)
    def test_restart_tenant_container_rejects_invalid_signature(self, mock_verify):
        response = self.client.post("/api/v1/cron/restart-tenant-container/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Invalid signature")


class ExpireTrialsEntitlementTest(TestCase):
    """Regression guards for the broadened entitlement query.

    The bug this prevents: production had 17 tenants with
    ``is_trial=False, status='active', no Stripe sub`` and trial_ends_at
    in the past. The earlier query filtered on ``is_trial=True`` so
    these ghost tenants were silently skipped on every daily sweep,
    accumulating LLM cost. The new query matches by ENTITLEMENT, not
    the ``is_trial`` flag.
    """

    def setUp(self):
        self.client = APIClient()

    def _make_tenant(self, *, suffix: str, **kwargs) -> Tenant:
        user = User.objects.create_user(username=f"ent-{suffix}", password="x")
        defaults = dict(
            user=user,
            status=Tenant.Status.ACTIVE,
            model_tier=Tenant.ModelTier.STARTER,
            container_id=f"oc-{suffix}",
            container_fqdn=f"oc-{suffix}.internal.azurecontainerapps.io",
        )
        defaults.update(kwargs)
        return Tenant.objects.create(**defaults)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.orchestrator.azure_client.hibernate_container_app", return_value=None)
    @patch("apps.cron.suspension.suspend_tenant_crons", return_value={"disabled": 3, "errors": 0})
    def test_matches_ghost_state_unentitled_active(self, mock_suspend, mock_hibernate, mock_verify):
        """Tenant with is_trial=False, active, no sub, trial_ended is matched."""
        ghost = self._make_tenant(
            suffix="ghost",
            is_trial=False,
            trial_ends_at=timezone.now() - timedelta(days=20),
            stripe_subscription_id="",
        )
        response = self.client.post("/api/v1/cron/expire-trials/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["updated"], 1)

        ghost.refresh_from_db()
        self.assertEqual(ghost.status, Tenant.Status.SUSPENDED)
        self.assertFalse(ghost.is_trial)
        mock_suspend.assert_called_once()
        mock_hibernate.assert_called_once_with(ghost.container_id)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.orchestrator.azure_client.hibernate_container_app", return_value=None)
    @patch("apps.cron.suspension.suspend_tenant_crons", return_value={"disabled": 0, "errors": 0})
    def test_matches_classic_trial_expired(self, mock_suspend, mock_hibernate, mock_verify):
        """Original behavior preserved: is_trial=True with trial_ended is matched."""
        trial_user = self._make_tenant(
            suffix="trial",
            is_trial=True,
            trial_ends_at=timezone.now() - timedelta(days=1),
            stripe_subscription_id="",
        )
        response = self.client.post("/api/v1/cron/expire-trials/")
        self.assertEqual(response.json()["updated"], 1)

        trial_user.refresh_from_db()
        self.assertEqual(trial_user.status, Tenant.Status.SUSPENDED)
        self.assertFalse(trial_user.is_trial)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.orchestrator.azure_client.hibernate_container_app", return_value=None)
    @patch("apps.cron.suspension.suspend_tenant_crons", return_value={"disabled": 0, "errors": 0})
    def test_skips_paid_tenant(self, mock_suspend, mock_hibernate, mock_verify):
        """Tenant with a Stripe subscription is skipped regardless of trial state."""
        paid = self._make_tenant(
            suffix="paid",
            is_trial=False,
            trial_ends_at=timezone.now() - timedelta(days=20),
            stripe_subscription_id="sub_real",
        )
        response = self.client.post("/api/v1/cron/expire-trials/")
        self.assertEqual(response.json()["updated"], 0)

        paid.refresh_from_db()
        self.assertEqual(paid.status, Tenant.Status.ACTIVE)
        mock_suspend.assert_not_called()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.orchestrator.azure_client.hibernate_container_app", return_value=None)
    @patch("apps.cron.suspension.suspend_tenant_crons", return_value={"disabled": 0, "errors": 0})
    def test_skips_active_trial(self, mock_suspend, mock_hibernate, mock_verify):
        """Tenant on a valid (unexpired) trial is skipped."""
        active_trial = self._make_tenant(
            suffix="onTrial",
            is_trial=True,
            trial_ends_at=timezone.now() + timedelta(days=5),
            stripe_subscription_id="",
        )
        response = self.client.post("/api/v1/cron/expire-trials/")
        self.assertEqual(response.json()["updated"], 0)

        active_trial.refresh_from_db()
        self.assertEqual(active_trial.status, Tenant.Status.ACTIVE)
        self.assertTrue(active_trial.is_trial)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.orchestrator.azure_client.hibernate_container_app", return_value=None)
    @patch("apps.cron.suspension.suspend_tenant_crons", return_value={"disabled": 0, "errors": 0})
    def test_reports_already_hibernated_separately(self, mock_suspend, mock_hibernate, mock_verify):
        """Ghost tenants that are already hibernated are counted separately."""
        ghost = self._make_tenant(
            suffix="ghosthibe",
            is_trial=False,
            trial_ends_at=timezone.now() - timedelta(days=20),
            stripe_subscription_id="",
            hibernated_at=timezone.now() - timedelta(days=7),
        )
        response = self.client.post("/api/v1/cron/expire-trials/")
        body = response.json()
        self.assertEqual(body["updated"], 1)
        self.assertEqual(body["already_hibernated"], 1)

        ghost.refresh_from_db()
        self.assertEqual(ghost.status, Tenant.Status.SUSPENDED)
