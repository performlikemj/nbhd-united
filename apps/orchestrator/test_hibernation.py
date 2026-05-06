"""Tests for hibernation wake flow.

Regression guards for the wake-time image refresh that fixes the
"hibernated tenants come back stale" bug discovered 2026-04-26, plus
the cron-capture snapshot/seed fallback that fixes the silent
wake-chain breakage when the gateway returns 404 (Azure revision
inactive at hibernation time).
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.cron.gateway_client import GatewayError
from apps.orchestrator.hibernation import _capture_tenant_cron_schedules, wake_hibernated_tenant
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class WakeHibernatedTenantImageRefreshTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Wake Refresh",
            telegram_chat_id=987654321,
        )
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-wake-test"
        self.tenant.container_fqdn = "oc-wake-test.internal"
        self.tenant.hibernated_at = timezone.now()
        self.tenant.save()

    @override_settings(
        OPENCLAW_IMAGE_TAG="newsha123",
        AZURE_ACR_SERVER="test.azurecr.io",
    )
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.update_container_image")
    def test_wake_refreshes_image_when_stale(
        self,
        mock_update_image,
        mock_wake,
        _mock_publish,
    ):
        """When tenant.container_image_tag != OPENCLAW_IMAGE_TAG, wake should
        push the new image (which auto-activates in single-revision mode) and
        skip the plain wake_container_app call.
        """
        self.tenant.container_image_tag = "oldsha456"
        self.tenant.save(update_fields=["container_image_tag"])

        result = wake_hibernated_tenant(self.tenant)

        self.assertTrue(result)
        mock_update_image.assert_called_once_with(
            "oc-wake-test",
            "test.azurecr.io/nbhd-openclaw:newsha123",
        )
        mock_wake.assert_not_called()

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.container_image_tag, "newsha123")
        self.assertIsNone(self.tenant.hibernated_at)

    @override_settings(
        OPENCLAW_IMAGE_TAG="samesha",
        AZURE_ACR_SERVER="test.azurecr.io",
    )
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=False)
    @patch("apps.orchestrator.azure_client.update_container_image")
    def test_wake_uses_plain_wake_when_image_already_current(
        self,
        mock_update_image,
        mock_ensure_mount,
        mock_wake,
        _mock_publish,
    ):
        """No image refresh when tenant is already on the desired tag —
        avoids creating an unnecessary new revision per wake. When the
        plugin-runtime-deps mount is already present, fall through to the
        plain wake call.
        """
        self.tenant.container_image_tag = "samesha"
        self.tenant.save(update_fields=["container_image_tag"])

        result = wake_hibernated_tenant(self.tenant)

        self.assertTrue(result)
        mock_ensure_mount.assert_called_once_with("oc-wake-test")
        mock_wake.assert_called_once_with("oc-wake-test")
        mock_update_image.assert_not_called()

    @override_settings(
        OPENCLAW_IMAGE_TAG="samesha",
        AZURE_ACR_SERVER="test.azurecr.io",
    )
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=True)
    @patch("apps.orchestrator.azure_client.update_container_image")
    def test_wake_skips_plain_wake_when_mount_was_added(
        self,
        mock_update_image,
        mock_ensure_mount,
        mock_wake,
        _mock_publish,
    ):
        """If ensure_plugin_runtime_deps_mount adds the mount, the resulting
        new revision auto-activates in single-revision mode — that wakes the
        container, so wake_container_app must not be called (would be a
        wasted second restart).
        """
        self.tenant.container_image_tag = "samesha"
        self.tenant.save(update_fields=["container_image_tag"])

        result = wake_hibernated_tenant(self.tenant)

        self.assertTrue(result)
        mock_ensure_mount.assert_called_once_with("oc-wake-test")
        mock_wake.assert_not_called()
        mock_update_image.assert_not_called()

    @override_settings(
        OPENCLAW_IMAGE_TAG="latest",
        AZURE_ACR_SERVER="test.azurecr.io",
    )
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=False)
    @patch("apps.orchestrator.azure_client.update_container_image")
    def test_wake_uses_plain_wake_when_image_tag_is_latest(
        self,
        mock_update_image,
        mock_ensure_mount,
        mock_wake,
        _mock_publish,
    ):
        """The string 'latest' is the un-pinned default — never use it as a
        refresh target since it would re-pull the same floating tag every wake.
        """
        self.tenant.container_image_tag = "oldsha"
        self.tenant.save(update_fields=["container_image_tag"])

        wake_hibernated_tenant(self.tenant)

        mock_ensure_mount.assert_called_once_with("oc-wake-test")
        mock_wake.assert_called_once_with("oc-wake-test")
        mock_update_image.assert_not_called()


class CaptureTenantCronSchedulesFallbackTest(TestCase):
    """Regression guards for the snapshot/seed fallback in
    ``_capture_tenant_cron_schedules``.

    The bug this prevents: when the per-tenant container's revision is
    inactive at the moment of hibernation, ``cron.list`` over the
    gateway returns Azure's HTML 404. Pre-fix, the function silently
    returned ``[]``, ``_schedule_next_cron_wake`` skipped, and the
    tenant wedged in hibernation forever. Post-fix, snapshot/seed
    fallback ensures wake is always armed.
    """

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Fallback Test",
            telegram_chat_id=123456789,
        )
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-fallback-test"
        self.tenant.container_fqdn = "oc-fallback-test.internal"
        self.tenant.save()

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_uses_live_response_when_gateway_succeeds(self, mock_invoke):
        live_jobs = [
            {"name": "Morning Briefing", "schedule": {"expr": "0 7 * * *", "tz": "UTC"}, "enabled": True},
            {"name": "Heartbeat", "schedule": {"expr": "*/15 * * * *", "tz": "UTC"}, "enabled": True},
        ]
        mock_invoke.return_value = {"jobs": live_jobs}

        result = _capture_tenant_cron_schedules(self.tenant)

        self.assertEqual(result, live_jobs)
        mock_invoke.assert_called_once_with(self.tenant, "cron.list", {"includeDisabled": False})

        # Snapshot persisted on success
        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.cron_jobs_snapshot)
        self.assertEqual(self.tenant.cron_jobs_snapshot["jobs"], live_jobs)
        self.assertIn("snapshot_at", self.tenant.cron_jobs_snapshot)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_falls_back_to_snapshot_when_gateway_fails(self, mock_invoke):
        snapshot_jobs = [
            {"name": "Morning Briefing", "schedule": {"expr": "0 7 * * *", "tz": "UTC"}, "enabled": True},
            {"name": "Disabled Job", "schedule": {"expr": "0 9 * * *", "tz": "UTC"}, "enabled": False},
        ]
        self.tenant.cron_jobs_snapshot = {
            "jobs": snapshot_jobs,
            "snapshot_at": timezone.now().isoformat(),
        }
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        mock_invoke.side_effect = GatewayError("404: <!DOCTYPE html>", status_code=404)

        result = _capture_tenant_cron_schedules(self.tenant)

        # Returns only enabled jobs from snapshot — matches live cron.list semantics
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Morning Briefing")
        mock_invoke.assert_called_once()

    @patch("apps.orchestrator.config_generator.build_cron_seed_jobs")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_falls_back_to_seed_when_gateway_fails_and_snapshot_empty(self, mock_invoke, mock_seed):
        mock_invoke.side_effect = GatewayError("404: <!DOCTYPE html>", status_code=404)

        seed_jobs = [
            {"name": "Morning Briefing", "schedule": {"expr": "0 7 * * *", "tz": "UTC"}, "enabled": True},
            {"name": "Evening Check-in", "schedule": {"expr": "0 21 * * *", "tz": "UTC"}, "enabled": True},
        ]
        mock_seed.return_value = seed_jobs

        # Snapshot left empty (default-dict {})
        self.tenant.cron_jobs_snapshot = {}
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        result = _capture_tenant_cron_schedules(self.tenant)

        self.assertEqual(result, seed_jobs)
        mock_seed.assert_called_once_with(self.tenant)

    @patch("apps.orchestrator.config_generator.build_cron_seed_jobs")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_returns_empty_when_all_fallbacks_fail(self, mock_invoke, mock_seed):
        mock_invoke.side_effect = GatewayError("502: bad_gateway", status_code=502)
        mock_seed.side_effect = RuntimeError("seed broken")

        self.tenant.cron_jobs_snapshot = {}
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        result = _capture_tenant_cron_schedules(self.tenant)

        self.assertEqual(result, [])
        mock_invoke.assert_called_once()
        mock_seed.assert_called_once()

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_no_container_fqdn_returns_empty_without_calling_gateway(self, mock_invoke):
        self.tenant.container_fqdn = ""
        self.tenant.save(update_fields=["container_fqdn"])

        result = _capture_tenant_cron_schedules(self.tenant)

        self.assertEqual(result, [])
        mock_invoke.assert_not_called()
