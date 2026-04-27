"""Tests for hibernation wake flow.

Regression guards for the wake-time image refresh that fixes the
"hibernated tenants come back stale" bug discovered 2026-04-26.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.orchestrator.hibernation import wake_hibernated_tenant
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
