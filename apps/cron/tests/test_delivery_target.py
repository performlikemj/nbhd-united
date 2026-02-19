from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant

User = get_user_model()


def _create_user_and_tenant(*, telegram_chat_id: int | None = 12345):
    user = User.objects.create_user(
        username=f"cronuser{User.objects.count()}",
        password="testpass123",
        telegram_chat_id=telegram_chat_id,
    )
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_id="oc-test",
        container_fqdn="test.internal",
    )
    return user, tenant


class CronDeliveryTargetInjectionTests(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_injects_telegram_to(self, mock_invoke):
        mock_invoke.return_value = {"status": "ok"}
        payload = {
            "name": "Morning Briefing",
            "delivery": {"mode": "announce", "channel": "telegram"},
            "schedule": {"kind": "cron", "expr": "* * * * *", "tz": "UTC"},
        }

        response = self.client.post("/api/v1/cron-jobs/", payload, format="json")
        self.assertEqual(response.status_code, 201)
        job_arg = mock_invoke.call_args.args[2]
        self.assertEqual(job_arg["job"]["delivery"]["to"], "12345")
        self.assertEqual(job_arg["job"]["delivery"], {"mode": "announce", "channel": "telegram", "to": "12345"})

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_no_inject_when_mode_none(self, mock_invoke):
        mock_invoke.return_value = {"status": "ok"}
        payload = {
            "name": "Morning Briefing",
            "delivery": {"mode": "none", "channel": "telegram"},
            "schedule": {"kind": "cron", "expr": "* * * * *", "tz": "UTC"},
        }

        response = self.client.post("/api/v1/cron-jobs/", payload, format="json")
        self.assertEqual(response.status_code, 201)
        job_arg = mock_invoke.call_args.args[2]
        self.assertEqual(job_arg["job"]["delivery"], {"mode": "none", "channel": "telegram"})

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_no_inject_without_chat_id(self, mock_invoke):
        self.user, self.tenant = _create_user_and_tenant(telegram_chat_id=None)
        self.client.force_authenticate(user=self.user)

        mock_invoke.return_value = {"status": "ok"}
        payload = {
            "name": "Morning Briefing",
            "delivery": {"mode": "announce", "channel": "telegram"},
            "schedule": {"kind": "cron", "expr": "* * * * *", "tz": "UTC"},
        }

        response = self.client.post("/api/v1/cron-jobs/", payload, format="json")
        self.assertEqual(response.status_code, 201)
        job_arg = mock_invoke.call_args.args[2]
        self.assertNotIn("to", job_arg["job"]["delivery"])

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_create_preserves_existing_to(self, mock_invoke):
        mock_invoke.return_value = {"status": "ok"}
        payload = {
            "name": "Morning Briefing",
            "delivery": {"mode": "announce", "channel": "telegram", "to": "98765"},
            "schedule": {"kind": "cron", "expr": "* * * * *", "tz": "UTC"},
        }

        response = self.client.post("/api/v1/cron-jobs/", payload, format="json")
        self.assertEqual(response.status_code, 201)
        job_arg = mock_invoke.call_args.args[2]
        self.assertEqual(job_arg["job"]["delivery"], {"mode": "announce", "channel": "telegram", "to": "98765"})

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_update_injects_telegram_to(self, mock_invoke):
        mock_invoke.return_value = {"status": "ok"}
        payload = {
            "delivery": {"mode": "announce", "channel": "telegram"},
        }

        response = self.client.patch(
            "/api/v1/cron-jobs/Morning Briefing/",
            payload,
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        patch_arg = mock_invoke.call_args.args[2]
        self.assertEqual(patch_arg["patch"]["delivery"], {"mode": "announce", "channel": "telegram", "to": "12345"})

    @patch("apps.cron.tenant_views.invoke_gateway_tool")
    def test_update_no_delivery_no_inject(self, mock_invoke):
        mock_invoke.return_value = {"status": "ok"}
        payload = {
            "schedule": {"kind": "cron", "expr": "* * * * *", "tz": "UTC"},
        }

        response = self.client.patch(
            "/api/v1/cron-jobs/Morning Briefing/",
            payload,
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        patch_arg = mock_invoke.call_args.args[2]
        self.assertEqual(patch_arg, {
            "jobId": "Morning Briefing",
            "patch": payload,
        })
