"""Regression coverage for Fuel's activation-only welcome flow."""

from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.fuel.tasks import schedule_fuel_welcome_task
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class FuelWelcomeActivationTransitionTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Fresh Fuel", telegram_chat_id=908001)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "fresh-fuel-container"
        self.tenant.container_fqdn = "fresh-fuel.example.com"
        self.tenant.save(update_fields=["status", "container_id", "container_fqdn"])

        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    @patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={})
    @patch("apps.cron.gateway_client.cron_get", return_value=None)
    @patch("apps.orchestrator.azure_client.restart_container_app")
    def test_fresh_enable_restart_schedules_exactly_once_and_stamps(
        self,
        mock_restart,
        mock_cron_get,
        mock_invoke,
    ):
        with patch("apps.cron.publish.publish_task") as mock_publish:
            enabled = self.client.patch(
                "/api/v1/fuel/settings/",
                {"fuel_enabled": True},
                format="json",
            )

        self.assertEqual(enabled.status_code, 200)
        self.assertTrue(enabled.data["restart_required"])
        mock_publish.assert_called_once_with("apply_single_tenant_config", str(self.tenant.id))

        with patch("apps.cron.publish.publish_task") as mock_publish:
            restarted = self.client.post("/api/v1/fuel/restart/")

        self.assertEqual(restarted.status_code, 200)
        self.assertEqual(restarted.data, {"restarted": True})
        mock_restart.assert_called_once_with("fresh-fuel-container")
        mock_publish.assert_called_once_with(
            "schedule_fuel_welcome",
            str(self.tenant.id),
            delay_seconds=90,
        )

        schedule_fuel_welcome_task(str(self.tenant.id))
        schedule_fuel_welcome_task(str(self.tenant.id))

        self.tenant.refresh_from_db()
        self.assertIn("fuel", self.tenant.welcomes_sent)
        mock_cron_get.assert_called_once()
        mock_invoke.assert_called_once()
        self.assertEqual(mock_invoke.call_args.args[1], "cron.add")
