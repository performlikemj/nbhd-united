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
        mock_invoke.return_value = {"name": "New Job", "enabled": True}
        resp = self.client.post(
            "/api/v1/cron-jobs/",
            {"name": "New Job", "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        mock_invoke.assert_called_once()
        call_args = mock_invoke.call_args
        self.assertEqual(call_args[0][1], "cron.add")

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
        self.assertEqual(call_args[0][2]["name"], "Morning Briefing")

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_delete_cron_job(self, mock_invoke):
        mock_invoke.return_value = {}
        resp = self.client.delete("/api/v1/cron-jobs/Morning Briefing/")
        self.assertEqual(resp.status_code, 204)
        mock_invoke.assert_called_once_with(
            self.tenant, "cron.remove", {"name": "Morning Briefing"},
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
        self.assertEqual(call_args[0][2]["enabled"], False)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_toggle_requires_enabled_field(self, mock_invoke):
        resp = self.client.post(
            "/api/v1/cron-jobs/Morning Briefing/toggle/", {}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
        mock_invoke.assert_not_called()


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
