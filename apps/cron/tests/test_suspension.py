"""Tests for cron job suspension and resumption lifecycle."""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.tenants.models import Tenant, User
from apps.cron.suspension import resume_tenant_crons, suspend_tenant_crons


class CronSuspensionTestBase(TestCase):
    """Shared setup for suspension tests."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser",
            password="testpass",
            telegram_chat_id=12345,
        )
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            container_id="oc-test-container",
            container_fqdn="oc-test.internal",
        )

    MOCK_JOBS = [
        {"jobId": "job-1", "name": "Morning Briefing", "enabled": True},
        {"jobId": "job-2", "name": "Evening Check-in", "enabled": True},
        {"jobId": "job-3", "name": "My Reminder", "enabled": True},
        {"jobId": "job-4", "name": "Background Tasks", "enabled": False},  # already disabled
    ]


class SuspendTenantCronsTest(CronSuspensionTestBase):
    """Tests for suspend_tenant_crons."""

    @patch("apps.cron.suspension.invoke_gateway_tool")
    def test_disables_all_enabled_jobs(self, mock_invoke):
        mock_invoke.side_effect = [
            {"jobs": self.MOCK_JOBS},  # cron.list
            {},  # disable job-1
            {},  # disable job-2
            {},  # disable job-3
        ]

        result = suspend_tenant_crons(self.tenant)

        self.assertEqual(result["disabled"], 3)
        self.assertEqual(result["already_disabled"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertIn("Morning Briefing", result["job_names"])
        self.assertIn("My Reminder", result["job_names"])

    @patch("apps.cron.suspension.invoke_gateway_tool")
    def test_skips_already_disabled_jobs(self, mock_invoke):
        all_disabled = [
            {"jobId": "job-1", "name": "Morning Briefing", "enabled": False},
            {"jobId": "job-2", "name": "Evening Check-in", "enabled": False},
        ]
        mock_invoke.return_value = {"jobs": all_disabled}

        result = suspend_tenant_crons(self.tenant)

        self.assertEqual(result["disabled"], 0)
        self.assertEqual(result["already_disabled"], 2)
        # Only the list call should have been made
        mock_invoke.assert_called_once()

    @patch("apps.cron.suspension.invoke_gateway_tool")
    def test_handles_gateway_list_error(self, mock_invoke):
        from apps.cron.gateway_client import GatewayError
        mock_invoke.side_effect = GatewayError("Container unreachable")

        result = suspend_tenant_crons(self.tenant)

        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["disabled"], 0)

    @patch("apps.cron.suspension.invoke_gateway_tool")
    def test_handles_individual_disable_error(self, mock_invoke):
        from apps.cron.gateway_client import GatewayError
        mock_invoke.side_effect = [
            {"jobs": [{"jobId": "job-1", "name": "Test Job", "enabled": True}]},
            GatewayError("timeout"),
        ]

        result = suspend_tenant_crons(self.tenant)

        self.assertEqual(result["disabled"], 0)
        self.assertEqual(result["errors"], 1)

    def test_no_fqdn_returns_empty(self):
        self.tenant.container_fqdn = ""
        self.tenant.save()

        result = suspend_tenant_crons(self.tenant)

        self.assertEqual(result["disabled"], 0)


class ResumeTenantCronsTest(CronSuspensionTestBase):
    """Tests for resume_tenant_crons."""

    @patch("apps.cron.suspension.invoke_gateway_tool")
    def test_enables_all_disabled_jobs(self, mock_invoke):
        all_disabled = [
            {"jobId": "job-1", "name": "Morning Briefing", "enabled": False},
            {"jobId": "job-2", "name": "Evening Check-in", "enabled": False},
            {"jobId": "job-3", "name": "My Reminder", "enabled": False},
        ]
        mock_invoke.side_effect = [
            {"jobs": all_disabled},  # cron.list
            {},  # enable job-1
            {},  # enable job-2
            {},  # enable job-3
        ]

        result = resume_tenant_crons(self.tenant)

        self.assertEqual(result["enabled"], 3)
        self.assertEqual(result["already_enabled"], 0)
        self.assertEqual(result["errors"], 0)

    @patch("apps.cron.suspension.invoke_gateway_tool")
    def test_skips_already_enabled_jobs(self, mock_invoke):
        mixed = [
            {"jobId": "job-1", "name": "Morning Briefing", "enabled": True},
            {"jobId": "job-2", "name": "Evening Check-in", "enabled": False},
        ]
        mock_invoke.side_effect = [
            {"jobs": mixed},
            {},  # enable job-2
        ]

        result = resume_tenant_crons(self.tenant)

        self.assertEqual(result["enabled"], 1)
        self.assertEqual(result["already_enabled"], 1)

    @patch("apps.cron.suspension.invoke_gateway_tool")
    def test_handles_gateway_error(self, mock_invoke):
        from apps.cron.gateway_client import GatewayError
        mock_invoke.side_effect = GatewayError("Container still starting")

        result = resume_tenant_crons(self.tenant)

        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["enabled"], 0)

    def test_no_fqdn_returns_empty(self):
        self.tenant.container_fqdn = ""
        self.tenant.save()

        result = resume_tenant_crons(self.tenant)

        self.assertEqual(result["enabled"], 0)


class ExpireTrialsCronIntegrationTest(CronSuspensionTestBase):
    """Test expire_trials disables crons and hibernates."""

    @patch("apps.orchestrator.azure_client.hibernate_container_app")
    @patch("apps.cron.suspension.suspend_tenant_crons")
    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_expire_trials_disables_crons_and_hibernates(
        self, mock_verify, mock_suspend, mock_hibernate
    ):
        from django.test import RequestFactory
        from django.utils import timezone as tz
        from apps.cron.views import expire_trials

        self.tenant.is_trial = True
        self.tenant.trial_ends_at = tz.now() - tz.timedelta(hours=1)
        self.tenant.save()

        mock_suspend.return_value = {"disabled": 3, "already_disabled": 0, "errors": 0}

        factory = RequestFactory()
        request = factory.post("/api/v1/cron/expire-trials/")

        response = expire_trials(request)

        self.assertEqual(response.status_code, 200)
        import json
        data = json.loads(response.content)
        self.assertEqual(data["updated"], 1)
        self.assertEqual(data["crons_disabled"], 3)
        self.assertEqual(data["hibernated"], 1)

        mock_suspend.assert_called_once_with(self.tenant)
        mock_hibernate.assert_called_once_with("oc-test-container")

        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.is_trial)
        self.assertEqual(self.tenant.status, Tenant.Status.SUSPENDED)


class CronDeliveryBlockTest(CronSuspensionTestBase):
    """Test CronDeliveryView blocks suspended tenants."""

    @patch("apps.router.cron_delivery.validate_internal_runtime_request")
    def test_blocks_suspended_tenant(self, mock_auth):
        from rest_framework.test import APIRequestFactory
        from apps.router.cron_delivery import CronDeliveryView

        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.save()

        factory = APIRequestFactory()
        request = factory.post(
            f"/api/v1/cron/deliver/{self.tenant.id}/",
            {"message": "Evening check-in time 🌙"},
            format="json",
        )
        request.META["HTTP_X_NBHD_INTERNAL_KEY"] = "test"
        request.META["HTTP_X_NBHD_TENANT_ID"] = str(self.tenant.id)

        view = CronDeliveryView.as_view()
        response = view(request, tenant_id=self.tenant.id)

        self.assertEqual(response.status_code, 200)  # 200 to prevent retries
        self.assertEqual(response.data["status"], "blocked")
        self.assertEqual(response.data["reason"], "tenant_not_active")

    @patch("apps.router.cron_delivery.validate_internal_runtime_request")
    def test_allows_active_tenant(self, mock_auth):
        from rest_framework.test import APIRequestFactory
        from apps.router.cron_delivery import CronDeliveryView

        # Active tenant should proceed past the status check
        # (will fail later on missing bot token, but that's fine for this test)
        factory = APIRequestFactory()
        request = factory.post(
            f"/api/v1/cron/deliver/{self.tenant.id}/",
            {"message": "Hello!"},
            format="json",
        )
        request.META["HTTP_X_NBHD_INTERNAL_KEY"] = "test"
        request.META["HTTP_X_NBHD_TENANT_ID"] = str(self.tenant.id)

        view = CronDeliveryView.as_view()
        response = view(request, tenant_id=self.tenant.id)

        # Should NOT be blocked — may fail on telegram/rate limit, but not "blocked"
        if hasattr(response, 'data') and isinstance(response.data, dict):
            self.assertNotEqual(response.data.get("status"), "blocked")


class RuntimeProfileLocationTest(CronSuspensionTestBase):
    """Tests for location fields on the runtime profile endpoint."""

    def _patch_profile(self, data):
        from rest_framework.test import APIRequestFactory
        from apps.integrations.runtime_views import RuntimeProfileUpdateView

        factory = APIRequestFactory()
        request = factory.patch(
            f"/api/v1/integrations/runtime/{self.tenant.id}/profile/",
            data,
            format="json",
        )
        request.META["HTTP_X_NBHD_INTERNAL_KEY"] = "test-key"
        request.META["HTTP_X_NBHD_TENANT_ID"] = str(self.tenant.id)

        view = RuntimeProfileUpdateView.as_view()
        return view(request, tenant_id=self.tenant.id)

    @patch("apps.integrations.runtime_views._internal_auth_or_401", return_value=None)
    def test_set_location_city(self, mock_auth):
        response = self._patch_profile({"location_city": "Brooklyn"})
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.location_city, "Brooklyn")

    @patch("apps.integrations.runtime_views._internal_auth_or_401", return_value=None)
    def test_set_location_with_coordinates(self, mock_auth):
        response = self._patch_profile({
            "location_city": "Osaka",
            "location_lat": 34.69,
            "location_lon": 135.50,
        })
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.location_city, "Osaka")
        self.assertAlmostEqual(self.user.location_lat, 34.69)
        self.assertAlmostEqual(self.user.location_lon, 135.50)
        self.assertIn("location_city", response.data["updated"])
        self.assertIn("location_lat", response.data["updated"])

    @patch("apps.integrations.runtime_views._internal_auth_or_401", return_value=None)
    def test_invalid_coordinates_returns_400(self, mock_auth):
        response = self._patch_profile({
            "location_lat": 91.0,  # out of range
            "location_lon": 135.50,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("invalid_coordinates", response.data.get("error", ""))

    @patch("apps.integrations.runtime_views._internal_auth_or_401", return_value=None)
    def test_response_includes_location_fields(self, mock_auth):
        response = self._patch_profile({"location_city": "London"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["location_city"], "London")
        self.assertIn("location_lat", response.data)
        self.assertIn("location_lon", response.data)
