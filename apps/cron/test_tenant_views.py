"""Tests for tenant-facing cron job API."""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User


def _create_user_and_tenant(*, active=True):
    user = User.objects.create_user(username="cronuser", password="testpass123")
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE if active else Tenant.Status.PENDING,
        container_id="oc-test" if active else "",
        container_fqdn="oc-test.internal.azurecontainerapps.io" if active else "",
    )
    return user, tenant


class CronJobListCreateTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_list_cron_jobs(self, mock_invoke):
        mock_invoke.return_value = {
            "jobs": [
                {"name": "Morning Briefing", "enabled": True},
                {"name": "Evening Check-in", "enabled": True},
            ],
        }
        resp = self.client.get("/api/v1/cron-jobs/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["jobs"]), 2)
        mock_invoke.assert_called_once_with(self.tenant, "cron.list", {})

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_cron_job(self, mock_invoke):
        # First call: cron.list (cap check), second call: cron.add
        mock_invoke.side_effect = [
            {"jobs": []},  # cron.list returns empty
            {"name": "New Job", "enabled": True},  # cron.add result
        ]
        resp = self.client.post(
            "/api/v1/cron-jobs/",
            {"name": "New Job", "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(mock_invoke.call_count, 2)
        # First call: cap check
        self.assertEqual(mock_invoke.call_args_list[0][0][1], "cron.list")
        # Second call: actual create
        self.assertEqual(mock_invoke.call_args_list[1][0][1], "cron.add")
        self.assertEqual(mock_invoke.call_args_list[1][0][2], {
            "job": {"name": "New Job", "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"}},
        })

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_cron_job_rejected_at_cap(self, mock_invoke):
        """Creating a job when at the cap returns 409."""
        mock_invoke.return_value = {
            "jobs": [{"name": f"Job {i}"} for i in range(10)],
        }
        resp = self.client.post(
            "/api/v1/cron-jobs/",
            {"name": "One More", "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        # Only cron.list should have been called (no cron.add)
        mock_invoke.assert_called_once()

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_cron_job_duplicate_name_rejected(self, mock_invoke):
        """Creating a job with a duplicate name returns 409."""
        mock_invoke.return_value = {
            "jobs": [{"name": "Morning Briefing"}],
        }
        resp = self.client.post(
            "/api/v1/cron-jobs/",
            {"name": "Morning Briefing", "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        mock_invoke.assert_called_once()

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_requires_name(self, mock_invoke):
        resp = self.client.post("/api/v1/cron-jobs/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        mock_invoke.assert_not_called()

    def test_unauthenticated_request_rejected(self):
        client = APIClient()
        resp = client.get("/api/v1/cron-jobs/")
        self.assertEqual(resp.status_code, 401)


class CronJobDetailTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_update_cron_job(self, mock_invoke):
        mock_invoke.return_value = {"name": "Morning Briefing", "enabled": True}
        resp = self.client.patch(
            "/api/v1/cron-jobs/Morning Briefing/",
            {"schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        call_args = mock_invoke.call_args
        self.assertEqual(call_args[0][1], "cron.update")
        self.assertEqual(
            call_args[0][2],
            {
                "jobId": "Morning Briefing",
                "patch": {
                    "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"},
                },
            },
        )

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_delete_cron_job(self, mock_invoke):
        mock_invoke.return_value = {}
        resp = self.client.delete("/api/v1/cron-jobs/Morning Briefing/")
        self.assertEqual(resp.status_code, 204)
        mock_invoke.assert_called_once_with(
            self.tenant,
            "cron.remove",
            {"jobId": "Morning Briefing"},
        )


class CronJobToggleTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_toggle_cron_job(self, mock_invoke):
        mock_invoke.return_value = {"name": "Morning Briefing", "enabled": False}
        resp = self.client.post(
            "/api/v1/cron-jobs/Morning Briefing/toggle/",
            {"enabled": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        call_args = mock_invoke.call_args
        self.assertEqual(call_args[0][1], "cron.update")
        self.assertEqual(
            call_args[0][2],
            {
                "jobId": "Morning Briefing",
                "patch": {"enabled": False},
            },
        )

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_toggle_requires_enabled_field(self, mock_invoke):
        resp = self.client.post(
            "/api/v1/cron-jobs/Morning Briefing/toggle/", {}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
        mock_invoke.assert_not_called()


class HiddenSystemCronsTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_list_hides_system_crons(self, mock_invoke):
        mock_invoke.return_value = {
            "jobs": [
                {"name": "Morning Briefing", "enabled": True},
                {"name": "Evening Check-in", "enabled": True},
                {"name": "Background Tasks", "enabled": True},
                {"name": "Heartbeat Check-in", "enabled": True},
                {"name": "Week Ahead Review", "enabled": True},
            ],
        }
        resp = self.client.get("/api/v1/cron-jobs/")
        self.assertEqual(resp.status_code, 200)
        names = [j["name"] for j in resp.json()["jobs"]]
        self.assertNotIn("Background Tasks", names)
        self.assertNotIn("Heartbeat Check-in", names)
        # User-visible system crons should still appear
        self.assertIn("Morning Briefing", names)
        self.assertEqual(len(names), 3)

    def test_delete_blocked_for_system_crons(self):
        resp = self.client.delete("/api/v1/cron-jobs/Background Tasks/")
        self.assertEqual(resp.status_code, 403)

    def test_delete_blocked_for_heartbeat_checkin(self):
        resp = self.client.delete("/api/v1/cron-jobs/Heartbeat Check-in/")
        self.assertEqual(resp.status_code, 403)

    def test_patch_blocked_for_system_crons(self):
        resp = self.client.patch(
            "/api/v1/cron-jobs/Background Tasks/",
            {"enabled": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_toggle_blocked_for_system_crons(self):
        resp = self.client.post(
            "/api/v1/cron-jobs/Heartbeat Check-in/toggle/",
            {"enabled": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_bulk_delete_blocked_for_system_crons(self):
        resp = self.client.post(
            "/api/v1/cron-jobs/bulk-delete/",
            {"ids": ["Background Tasks", "Morning Briefing"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Background Tasks", resp.json()["detail"])


class InactiveTenantTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant(active=False)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_inactive_tenant_returns_502(self, mock_invoke):
        resp = self.client.get("/api/v1/cron-jobs/")
        self.assertEqual(resp.status_code, 502)
        mock_invoke.assert_not_called()
