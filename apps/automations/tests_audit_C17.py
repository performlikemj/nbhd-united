"""Regression tests for fix cluster C17 (FA-0020, FA-0026)."""

from __future__ import annotations

from datetime import time, timedelta
from unittest.mock import AsyncMock, patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .models import Automation, AutomationRun
from .services import (
    STALE_RUNNING_THRESHOLD,
    _build_idempotency_key,
    execute_automation,
)


def _make_automation(tenant: Tenant, **overrides) -> Automation:
    defaults = {
        "kind": Automation.Kind.DAILY_BRIEF,
        "status": Automation.Status.ACTIVE,
        "timezone": "UTC",
        "schedule_type": Automation.ScheduleType.DAILY,
        "schedule_time": time(9, 0),
        "schedule_days": [],
        "next_run_at": timezone.now() + timedelta(hours=1),
    }
    defaults.update(overrides)
    return Automation.objects.create(tenant=tenant, **defaults)


class ManualRunFailureStatusCodeTest(TestCase):
    """FA-0020: a manual run that fails to dispatch must not return HTTP 201."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Manual Run User", telegram_chat_id=990001)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def test_manual_run_returns_502_when_dispatch_fails(self):
        # Tenant lacks an active container endpoint -> _dispatch_to_openclaw
        # raises AutomationExecutionError, which execute_automation swallows
        # into a FAILED run rather than re-raising.
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_fqdn = ""
        self.tenant.save(update_fields=["status", "container_fqdn", "updated_at"])

        automation = _make_automation(self.tenant)

        response = self.client.post(f"/api/v1/automations/{automation.id}/run/")

        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["status"], AutomationRun.Status.FAILED)
        self.assertTrue(body["error_message"])

    @patch("apps.automations.services.forward_to_openclaw", new_callable=AsyncMock)
    def test_manual_run_still_returns_201_on_success(self, mock_forward):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_fqdn = "oc-manual.internal.azurecontainerapps.io"
        self.tenant.save(update_fields=["status", "container_fqdn", "updated_at"])
        mock_forward.return_value = {"ok": True}

        automation = _make_automation(self.tenant)

        response = self.client.post(f"/api/v1/automations/{automation.id}/run/")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], AutomationRun.Status.SUCCEEDED)


class StaleRunningReDispatchTest(TestCase):
    """FA-0026: a stranded RUNNING run must not permanently wedge the schedule."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Schedule User", telegram_chat_id=990002)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_fqdn = "oc-stale.internal.azurecontainerapps.io"
        self.tenant.save(update_fields=["status", "container_fqdn", "updated_at"])

    @patch("apps.automations.services.forward_to_openclaw", new_callable=AsyncMock)
    def test_stale_running_row_is_redispatched_and_schedule_advances(self, mock_forward):
        mock_forward.return_value = {"ok": True}

        scheduled_for = timezone.now() - timedelta(minutes=5)
        original_next_run = scheduled_for
        automation = _make_automation(self.tenant, next_run_at=original_next_run)

        # Simulate a prior dispatch that was hard-killed after marking RUNNING
        # but before dispatching / advancing next_run_at. The idempotency key
        # is window-deterministic for the same scheduled_for.
        idempotency_key = _build_idempotency_key(automation, AutomationRun.TriggerSource.SCHEDULE, scheduled_for)
        stranded = AutomationRun.objects.create(
            automation=automation,
            tenant=self.tenant,
            status=AutomationRun.Status.RUNNING,
            trigger_source=AutomationRun.TriggerSource.SCHEDULE,
            scheduled_for=scheduled_for,
            started_at=timezone.now() - (STALE_RUNNING_THRESHOLD + timedelta(minutes=1)),
            idempotency_key=idempotency_key,
        )

        run = execute_automation(
            automation=automation,
            trigger_source=AutomationRun.TriggerSource.SCHEDULE,
            scheduled_for=scheduled_for,
        )

        # Same row re-dispatched (not a new one), now succeeded.
        self.assertEqual(run.id, stranded.id)
        self.assertEqual(run.status, AutomationRun.Status.SUCCEEDED)
        mock_forward.assert_awaited_once()

        # Schedule advanced so the next tick stops re-selecting this automation.
        automation.refresh_from_db()
        self.assertGreater(automation.next_run_at, original_next_run)

    @patch("apps.automations.services.forward_to_openclaw", new_callable=AsyncMock)
    def test_fresh_running_row_still_short_circuits(self, mock_forward):
        mock_forward.return_value = {"ok": True}

        scheduled_for = timezone.now()
        automation = _make_automation(self.tenant, next_run_at=scheduled_for)
        idempotency_key = _build_idempotency_key(automation, AutomationRun.TriggerSource.SCHEDULE, scheduled_for)
        in_flight = AutomationRun.objects.create(
            automation=automation,
            tenant=self.tenant,
            status=AutomationRun.Status.RUNNING,
            trigger_source=AutomationRun.TriggerSource.SCHEDULE,
            scheduled_for=scheduled_for,
            started_at=timezone.now(),
            idempotency_key=idempotency_key,
        )

        run = execute_automation(
            automation=automation,
            trigger_source=AutomationRun.TriggerSource.SCHEDULE,
            scheduled_for=scheduled_for,
        )

        # A genuinely concurrent in-flight run is not re-dispatched.
        self.assertEqual(run.id, in_flight.id)
        self.assertEqual(run.status, AutomationRun.Status.RUNNING)
        mock_forward.assert_not_awaited()
