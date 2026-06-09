from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User


class TenantConfigVersionBumpTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="config-bump-user",
            password="testpass123",
        )
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_bump_pending_config_increments(self):
        self.tenant.pending_config_version = 4
        self.tenant.save(update_fields=["pending_config_version"])

        self.tenant.bump_pending_config()
        self.tenant.refresh_from_db()

        self.assertEqual(self.tenant.pending_config_version, 5)

    def test_update_preferences_patch_bumps_pending_config(self):
        response = self.client.patch(
            "/api/v1/tenants/preferences/",
            {"agent_persona": "neighbor"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pending_config_version, 1)

    @patch("apps.orchestrator.services.update_tenant_config")
    def test_profile_timezone_patch_bumps_pending_config(self, mock_update_tenant_config):
        self.tenant.container_id = "oc-test"
        self.tenant.save(update_fields=["container_id"])

        response = self.client.patch(
            "/api/v1/tenants/profile/",
            {"timezone": "Asia/Tokyo"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        # Caller bumps once; the deferral helper doesn't bump (it only writes
        # the file-share copy on the deferred path). One logical change → one
        # version bump.
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pending_config_version, 1)
        mock_update_tenant_config.assert_called_once_with(str(self.tenant.id))

    def test_refresh_config_view_indicates_pending_update(self):
        self.tenant.pending_config_version = 2
        self.tenant.config_version = 1
        self.tenant.save(update_fields=["pending_config_version", "config_version"])

        response = self.client.get("/api/v1/tenants/refresh-config/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["has_pending_update"])


class PreferredModelImmediateApplyTests(TestCase):
    """The picker enqueues an immediate apply_single_tenant_config task so a
    model switch lands within ~30s instead of waiting for the next hourly
    apply-pending-configs cron (which also skips active tenants via the
    15-min idle filter)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="picker-user",
            password="testpass123",
        )
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            container_id="oc-test",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.publish.publish_task")
    def test_preferred_model_patch_enqueues_apply(self, mock_publish):
        response = self.client.patch(
            "/api/v1/tenants/settings/preferred-model/",
            {"preferred_model": "openrouter/deepseek/deepseek-v4-flash"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        mock_publish.assert_called_once_with(
            "apply_single_tenant_config",
            str(self.tenant.id),
            idempotency_key=f"apply-config-{self.tenant.id}",
        )

    @patch("apps.cron.publish.publish_task")
    def test_preferred_model_patch_skips_apply_when_no_container(self, mock_publish):
        self.tenant.container_id = ""
        self.tenant.save(update_fields=["container_id"])

        response = self.client.patch(
            "/api/v1/tenants/settings/preferred-model/",
            {"preferred_model": "openrouter/deepseek/deepseek-v4-flash"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    def test_preferred_model_patch_skips_apply_when_hibernated(self, mock_publish):
        from django.utils import timezone

        self.tenant.hibernated_at = timezone.now()
        self.tenant.save(update_fields=["hibernated_at"])

        response = self.client.patch(
            "/api/v1/tenants/settings/preferred-model/",
            {"preferred_model": "openrouter/deepseek/deepseek-v4-flash"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    def test_task_model_preferences_patch_enqueues_apply(self, mock_publish):
        response = self.client.patch(
            "/api/v1/tenants/settings/task-model-preferences/",
            {"task_model_preferences": {"morning_briefing": "openrouter/deepseek/deepseek-v4-flash"}},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        mock_publish.assert_called_once_with(
            "apply_single_tenant_config",
            str(self.tenant.id),
            idempotency_key=f"apply-config-{self.tenant.id}",
        )

    @patch("apps.cron.publish.publish_task", side_effect=Exception("qstash down"))
    def test_preferred_model_patch_swallows_publish_failure(self, mock_publish):
        """Publish failure is non-fatal — falls back to hourly cron."""
        response = self.client.patch(
            "/api/v1/tenants/settings/preferred-model/",
            {"preferred_model": "openrouter/deepseek/deepseek-v4-flash"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        mock_publish.assert_called_once()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.preferred_model, "openrouter/deepseek/deepseek-v4-flash")
