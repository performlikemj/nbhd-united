"""Generic runtime capture: every oc-* → Django endpoint emits one event.

Genericity is the whole point of hooking the middleware rather than each tool, so
these tests drive real endpoints from two different apps (core and platform_logs,
mounted under different URL prefixes and implemented in different modules) without
either app knowing telemetry exists.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key

from .models import ToolContractEvent

_TEST_INTERNAL_KEY = "test-internal-key"


def _make_tenant() -> Tenant:
    uid = uuid.uuid4().hex[:8]
    user = User.objects.create_user(
        username=f"test-{uid}",
        email=f"test-{uid}@example.com",
        password="testpass123",
    )
    return Tenant.objects.create(id=uuid.uuid4(), user=user)


@override_settings(NBHD_INTERNAL_API_KEY=_TEST_INTERNAL_KEY)
class RuntimeCaptureTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = _make_tenant()
        seed_internal_key(self.tenant)
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": _TEST_INTERNAL_KEY,
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _report_url(self) -> str:
        return reverse("runtime-platform-issue-report", kwargs={"tenant_id": self.tenant.id})

    def _core_summary_url(self) -> str:
        return reverse("core-runtime-summary", kwargs={"tenant_id": self.tenant.id})

    # --- app #1: core ---------------------------------------------------------

    def test_core_runtime_endpoint_is_captured(self):
        resp = self.client.get(self._core_summary_url(), **self.headers)
        self.assertEqual(resp.status_code, 200)

        event = ToolContractEvent.objects.get()
        self.assertEqual(event.tool_name, "core-runtime-summary")
        self.assertEqual(event.outcome, ToolContractEvent.Outcome.ACCEPTED)
        self.assertEqual(event.tenant_id, self.tenant.id)
        self.assertEqual(event.namespace, "runtime")
        self.assertEqual(event.reason_code, "")
        self.assertEqual(event.detail["status"], 200)
        self.assertEqual(event.detail["method"], "GET")
        self.assertEqual(event.detail["app"], "core")
        self.assertIsNotNone(event.duration_ms)

    # --- app #2: platform_logs ------------------------------------------------

    def test_platform_logs_runtime_endpoint_is_captured(self):
        payload = {"category": "tool_error", "severity": "low", "summary": "timed out"}
        resp = self.client.post(self._report_url(), payload, format="json", **self.headers)
        self.assertEqual(resp.status_code, 201)

        event = ToolContractEvent.objects.get()
        self.assertEqual(event.tool_name, "runtime-platform-issue-report")
        self.assertEqual(event.outcome, ToolContractEvent.Outcome.ACCEPTED)
        self.assertEqual(event.tenant_id, self.tenant.id)
        self.assertEqual(event.detail["status"], 201)
        self.assertEqual(event.detail["method"], "POST")

    # --- outcome mapping ------------------------------------------------------

    def test_400_maps_to_rejected(self):
        resp = self.client.post(self._report_url(), {}, format="json", **self.headers)
        self.assertEqual(resp.status_code, 400)

        event = ToolContractEvent.objects.get()
        self.assertEqual(event.outcome, ToolContractEvent.Outcome.REJECTED)
        self.assertEqual(event.reason_code, "http_400")
        self.assertEqual(event.detail["status"], 400)

    def test_401_maps_to_rejected(self):
        resp = self.client.get(
            self._core_summary_url(),
            HTTP_X_NBHD_INTERNAL_KEY="wrong-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 401)

        event = ToolContractEvent.objects.get()
        self.assertEqual(event.outcome, ToolContractEvent.Outcome.REJECTED)
        self.assertEqual(event.reason_code, "http_401")
        # Attribution survives an auth failure — that is how you spot one tenant's
        # container failing to authenticate.
        self.assertEqual(event.tenant_id, self.tenant.id)

    def test_500_maps_to_error(self):
        self.client.raise_request_exception = False
        with patch(
            "apps.core.runtime_views.RuntimeCoreSummaryView.get",
            side_effect=RuntimeError("boom"),
        ):
            resp = self.client.get(self._core_summary_url(), **self.headers)
        self.assertEqual(resp.status_code, 500)

        event = ToolContractEvent.objects.get()
        self.assertEqual(event.tool_name, "core-runtime-summary")
        self.assertEqual(event.outcome, ToolContractEvent.Outcome.ERROR)
        self.assertEqual(event.reason_code, "http_500")

    # --- scoping --------------------------------------------------------------

    def test_exactly_one_event_per_call(self):
        for _ in range(3):
            self.client.get(self._core_summary_url(), **self.headers)
        self.assertEqual(ToolContractEvent.objects.count(), 3)

    def test_non_runtime_requests_are_not_captured(self):
        """Console/API traffic is not tool traffic — it must not pollute the rates."""
        self.client.get("/api/v1/tenants/me/")
        self.assertEqual(ToolContractEvent.objects.count(), 0)

    def test_capture_failure_does_not_break_the_endpoint(self):
        """Fail-open, end to end: a broken emitter still returns the tool's answer."""
        with (
            patch(
                "apps.platform_logs.middleware.emit_tool_event",
                side_effect=RuntimeError("telemetry exploded"),
            ),
            self.assertLogs("apps.platform_logs.middleware", level="WARNING"),
        ):
            resp = self.client.get(self._core_summary_url(), **self.headers)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ToolContractEvent.objects.count(), 0)
