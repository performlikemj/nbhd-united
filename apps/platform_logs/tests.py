"""Tests for platform issue logging API."""
from __future__ import annotations

import hashlib
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User
from .models import PlatformIssueLog


def _make_tenant(*, internal_key: str = "test-internal-key") -> tuple[Tenant, str]:
    """Create a tenant with a hashed internal API key. Returns (tenant, raw_key)."""
    key_hash = hashlib.sha256(internal_key.encode("utf-8")).hexdigest()
    uid = uuid.uuid4().hex[:8]
    user = User.objects.create_user(
        username=f"test-{uid}",
        email=f"test-{uid}@example.com",
        password="testpass123",
    )
    tenant = Tenant.objects.create(
        id=uuid.uuid4(),
        user=user,
        internal_api_key_hash=key_hash,
    )
    return tenant, internal_key


def _report_url(tenant_id: uuid.UUID) -> str:
    return reverse("runtime-platform-issue-report", kwargs={"tenant_id": tenant_id})


def _auth_headers(tenant: Tenant, key: str) -> dict:
    return {
        "HTTP_X_NBHD_INTERNAL_KEY": key,
        "HTTP_X_NBHD_TENANT_ID": str(tenant.id),
    }


class PlatformIssueReportTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant, self.key = _make_tenant()
        self.url = _report_url(self.tenant.id)
        self.headers = _auth_headers(self.tenant, self.key)
        self.valid_payload = {
            "category": "tool_error",
            "severity": "medium",
            "tool_name": "web_search",
            "summary": "Tool timed out after 30s",
        }

    def test_successful_report(self):
        resp = self.client.post(self.url, self.valid_payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["status"], "logged")
        self.assertTrue(uuid.UUID(data["id"]))
        self.assertEqual(PlatformIssueLog.objects.count(), 1)
        issue = PlatformIssueLog.objects.first()
        self.assertEqual(issue.category, "tool_error")
        self.assertEqual(issue.severity, "medium")
        self.assertEqual(issue.tool_name, "web_search")
        self.assertEqual(issue.tenant, self.tenant)

    def test_invalid_auth(self):
        headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "wrong-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        resp = self.client.post(self.url, self.valid_payload, format="json", **headers)
        self.assertIn(resp.status_code, [401, 403])
        self.assertEqual(PlatformIssueLog.objects.count(), 0)

    def test_missing_auth(self):
        resp = self.client.post(self.url, self.valid_payload, format="json")
        self.assertIn(resp.status_code, [401, 403])

    def test_rate_limiting(self):
        # Create 10 issues directly in DB
        for i in range(10):
            PlatformIssueLog.objects.create(
                tenant=self.tenant,
                category="other",
                summary=f"Issue {i}",
            )
        resp = self.client.post(self.url, self.valid_payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Rate limit", resp.json()["detail"])

    def test_deduplication(self):
        # First report succeeds
        resp = self.client.post(self.url, self.valid_payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 201)

        # Second identical report is deduplicated
        resp = self.client.post(self.url, self.valid_payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["deduplicated"])
        self.assertEqual(PlatformIssueLog.objects.count(), 1)

    def test_missing_required_fields(self):
        resp = self.client.post(self.url, {}, format="json", **self.headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("summary", resp.json())

    def test_summary_max_length(self):
        payload = {**self.valid_payload, "summary": "x" * 501}
        resp = self.client.post(self.url, payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("summary", resp.json())

    def test_no_dedup_without_tool_name(self):
        """Without tool_name, dedup doesn't apply — both reports created."""
        payload = {k: v for k, v in self.valid_payload.items() if k != "tool_name"}
        resp1 = self.client.post(self.url, payload, format="json", **self.headers)
        self.assertEqual(resp1.status_code, 201)
        resp2 = self.client.post(self.url, payload, format="json", **self.headers)
        self.assertEqual(resp2.status_code, 201)
        self.assertEqual(PlatformIssueLog.objects.count(), 2)
