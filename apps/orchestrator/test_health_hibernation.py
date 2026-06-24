"""Regression tests for hibernation-aware tenant health checks.

The 5-min health-check cron flags any non-200 /health response as unhealthy and
fires an alert to MJ's personal OpenClaw (which then read-times-out against a cold
gateway — see apps/cron/test_health_alert). An idle-hibernated tenant returns
Azure's 404 "Container App - Unavailable" splash, which used to trip that alert
every single tick.

check_tenant_health now treats that splash as benign ONLY when WE deliberately put
the tenant to sleep (``hibernated_at`` set). The IDENTICAL splash with
``hibernated_at`` unset means the container should be running but isn't
(crash-loop / failed image pull / 0 healthy replicas) and MUST stay unhealthy so a
real outage is not masked.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from apps.orchestrator.services import check_tenant_health
from apps.tenants.models import Tenant, User

# Azure's ingress splash for a container with no running replica. Contains the
# marker substring "Container App - Unavailable" the narrow detector keys on.
_AZURE_SPLASH = (
    "<!DOCTYPE html><html><head><title>Azure Container App - Unavailable</title></head><body>unavailable</body></html>"
)


def _make_tenant(*, hibernated: bool) -> Tenant:
    user = User.objects.create_user(
        username=f"health_{secrets.token_hex(4)}",
        password="x" * 32,
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-health.example.com",
        hibernated_at=timezone.now() if hibernated else None,
    )


def _resp(status_code: int, text: str) -> Mock:
    return Mock(status_code=status_code, text=text, elapsed=timedelta(milliseconds=42))


class CheckTenantHealthHibernationTests(TestCase):
    @patch("httpx.get")
    def test_hibernated_404_splash_is_healthy_and_flagged(self, mock_get):
        mock_get.return_value = _resp(404, _AZURE_SPLASH)
        tenant = _make_tenant(hibernated=True)

        result = check_tenant_health(str(tenant.id))

        # healthy=True keeps it OUT of the unhealthy list -> no alert fires.
        self.assertTrue(result["healthy"])
        self.assertTrue(result["hibernated"])
        self.assertTrue(result["checks"]["gateway"]["ok"])
        self.assertEqual(result["checks"]["gateway"]["status_code"], 404)
        # response_time_ms is omitted so `make health` surfaces the hibernated
        # detail instead of a latency number.
        self.assertNotIn("response_time_ms", result["checks"]["gateway"])

    @patch("httpx.get")
    def test_unhibernated_404_splash_stays_unhealthy(self, mock_get):
        # Same Azure splash, but we did NOT put this tenant to sleep -> it is a
        # crash-loop / failed-start / 0-replica fault and MUST still alert.
        mock_get.return_value = _resp(404, _AZURE_SPLASH)
        tenant = _make_tenant(hibernated=False)

        result = check_tenant_health(str(tenant.id))

        self.assertFalse(result["healthy"])
        self.assertFalse(result.get("hibernated"))
        self.assertFalse(result["checks"]["gateway"]["ok"])

    @patch("httpx.get")
    def test_hibernated_503_marker_stays_unhealthy(self, mock_get):
        # A 503 (even carrying the marker) is NOT the scaled-to-zero splash — it's
        # a waking/half-booted container. The narrow detector keys on 404, so this
        # stays unhealthy even though hibernated_at is set (over-alert > mask).
        mock_get.return_value = _resp(503, _AZURE_SPLASH)
        tenant = _make_tenant(hibernated=True)

        result = check_tenant_health(str(tenant.id))

        self.assertFalse(result["healthy"])
        self.assertFalse(result.get("hibernated"))

    @patch("httpx.get")
    def test_serving_200_is_healthy_not_hibernated(self, mock_get):
        # Answering 200 -> serving, even if hibernated_at happens to be set.
        mock_get.return_value = _resp(200, "ok")
        tenant = _make_tenant(hibernated=True)

        result = check_tenant_health(str(tenant.id))

        self.assertTrue(result["healthy"])
        self.assertFalse(result.get("hibernated"))
        self.assertIn("response_time_ms", result["checks"]["gateway"])
