"""Tests for bump_openclaw_version management command."""

from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class BumpOpenclawVersionTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Bump Test",
            telegram_chat_id=111222333,
        )
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-bump-test"
        self.tenant.container_fqdn = "oc-bump-test.internal"
        self.tenant.openclaw_version = "2026.4.5"
        self.tenant.save()

    @patch("apps.orchestrator.management.commands.bump_openclaw_version.update_container_image")
    @patch("apps.orchestrator.management.commands.bump_openclaw_version.update_tenant_config")
    def test_bump_single_tenant_updates_version_config_image(self, mock_config, mock_image):
        call_command(
            "bump_openclaw_version",
            oc_version="2026.4.15",
            tenant=str(self.tenant.id),
            image_tag="openclaw-2026.4.15",
        )
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_version, "2026.4.15")
        self.assertEqual(self.tenant.container_image_tag, "openclaw-2026.4.15")
        mock_config.assert_called_once_with(str(self.tenant.id))
        mock_image.assert_called_once()

    @patch("apps.orchestrator.management.commands.bump_openclaw_version.update_container_image")
    @patch("apps.orchestrator.management.commands.bump_openclaw_version.update_tenant_config")
    def test_bump_all_skips_already_bumped(self, mock_config, mock_image):
        self.tenant.openclaw_version = "2026.4.15"
        self.tenant.save()

        call_command(
            "bump_openclaw_version",
            oc_version="2026.4.15",
            all=True,
            image_tag="openclaw-2026.4.15",
        )
        mock_config.assert_not_called()
        mock_image.assert_not_called()

    @patch("apps.orchestrator.management.commands.bump_openclaw_version.update_container_image")
    @patch("apps.orchestrator.management.commands.bump_openclaw_version.update_tenant_config")
    def test_bump_rolls_back_on_config_failure(self, mock_config, mock_image):
        mock_config.side_effect = Exception("config push failed")

        call_command(
            "bump_openclaw_version",
            oc_version="2026.4.15",
            tenant=str(self.tenant.id),
            image_tag="openclaw-2026.4.15",
        )

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_version, "2026.4.5")

    @patch("apps.orchestrator.management.commands.bump_openclaw_version.update_container_image")
    @patch("apps.orchestrator.management.commands.bump_openclaw_version.update_tenant_config")
    def test_bump_rolls_back_on_image_failure(self, mock_config, mock_image):
        mock_image.side_effect = Exception("image deploy failed")

        call_command(
            "bump_openclaw_version",
            oc_version="2026.4.15",
            tenant=str(self.tenant.id),
            image_tag="openclaw-2026.4.15",
        )

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_version, "2026.4.5")

    @patch("apps.orchestrator.management.commands.bump_openclaw_version.update_container_image")
    @patch("apps.orchestrator.management.commands.bump_openclaw_version.update_tenant_config")
    def test_dry_run_changes_nothing(self, mock_config, mock_image):
        call_command(
            "bump_openclaw_version",
            oc_version="2026.4.15",
            tenant=str(self.tenant.id),
            image_tag="openclaw-2026.4.15",
            dry_run=True,
        )
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_version, "2026.4.5")
        mock_config.assert_not_called()
        mock_image.assert_not_called()

    @patch("apps.orchestrator.management.commands.bump_openclaw_version.update_container_image")
    @patch("apps.orchestrator.management.commands.bump_openclaw_version.update_tenant_config")
    def test_bump_skips_inactive_tenants(self, mock_config, mock_image):
        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.save()

        call_command(
            "bump_openclaw_version",
            oc_version="2026.4.15",
            tenant=str(self.tenant.id),
            image_tag="openclaw-2026.4.15",
        )
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_version, "2026.4.5")
        mock_config.assert_not_called()
