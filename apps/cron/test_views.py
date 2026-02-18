"""Tests for QStash cron trigger endpoints."""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from unittest.mock import patch

from apps.tenants.models import Tenant, User


def _create_tenant_with_config_state(*, active: bool = True, config_version: int = 0, pending_config_version: int = 0, last_message_at=None, has_container: bool = True, suffix: int = 0):
    user = User.objects.create_user(username=f"user-{pending_config_version}-{config_version}-{suffix}", password="testpass123")
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
    @patch("apps.cron.views.update_tenant_config")
    def test_apply_pending_configs_updates_idle_tenants_only(self, mock_update, mock_verify):
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

        mock_update.side_effect = lambda tenant_id: None

        response = self.client.post("/api/v1/cron/apply-pending-configs/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["updated"], 2)
        self.assertEqual(body["failed"], 0)
        self.assertEqual(body["evaluated"], 2)
        self.assertEqual(mock_update.call_count, 2)

        ready.refresh_from_db()
        stale.refresh_from_db()
        active_recent.refresh_from_db()
        updated_pending.refresh_from_db()

        self.assertEqual(ready.config_version, ready.pending_config_version)
        self.assertIsNotNone(ready.config_refreshed_at)
        self.assertEqual(stale.config_version, stale.pending_config_version)
        self.assertIsNotNone(stale.config_refreshed_at)
        self.assertEqual(active_recent.config_version, 1)
        self.assertEqual(updated_pending.config_version, 1)
        self.assertEqual(inactive.config_version, 0)


class CronAuthTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_apply_pending_configs_rejects_invalid_signature(self):
        response = self.client.post("/api/v1/cron/apply-pending-configs/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Invalid signature")
