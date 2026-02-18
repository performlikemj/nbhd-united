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

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pending_config_version, 1)
        mock_update_tenant_config.assert_called_once_with(str(self.tenant.id))

    def test_llm_config_put_bumps_pending_config(self):
        response = self.client.put(
            "/api/v1/tenants/settings/llm-config/",
            {
                "provider": "openai",
                "model_id": "openai/gpt-4o",
                "api_key": "sk-test-key",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pending_config_version, 1)
        self.assertEqual(response.data["provider"], "openai")

    def test_refresh_config_view_indicates_pending_update(self):
        self.tenant.pending_config_version = 2
        self.tenant.config_version = 1
        self.tenant.save(update_fields=["pending_config_version", "config_version"])

        response = self.client.get("/api/v1/tenants/refresh-config/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["has_pending_update"])
