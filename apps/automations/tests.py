"""Automation model, service, scheduler, and API tests."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .models import Automation, AutomationRun
from .scheduler import run_due_automations
from .services import (
    AutomationLimitError,
    _build_synthetic_telegram_update,
    compute_next_run_at,
    execute_automation,
)


class AutomationScheduleComputationTest(TestCase):
    def test_compute_next_run_daily_respects_timezone(self):
        reference = datetime(2026, 2, 12, 16, 30, tzinfo=dt_timezone.utc)  # 08:30 in Los Angeles
        next_run = compute_next_run_at(
            timezone_name="America/Los_Angeles",
            schedule_type=Automation.ScheduleType.DAILY,
            schedule_time=time(hour=9, minute=0),
            schedule_days=[],
            reference_utc=reference,
        )
        self.assertEqual(next_run, datetime(2026, 2, 12, 17, 0, tzinfo=dt_timezone.utc))

    def test_compute_next_run_weekly_uses_days(self):
        reference = datetime(2026, 2, 12, 16, 30, tzinfo=dt_timezone.utc)  # Thursday
        next_run = compute_next_run_at(
            timezone_name="UTC",
            schedule_type=Automation.ScheduleType.WEEKLY,
            schedule_time=time(hour=9, minute=0),
            schedule_days=[0],  # Monday
            reference_utc=reference,
        )
        self.assertEqual(next_run, datetime(2026, 2, 16, 9, 0, tzinfo=dt_timezone.utc))


class AutomationApiTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Automation User", telegram_chat_id=880001)
        self.other_tenant = create_tenant(display_name="Other User", telegram_chat_id=880002)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def _create_automation(self, *, tenant: Tenant | None = None, **overrides) -> Automation:
        target_tenant = tenant or self.tenant
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
        return Automation.objects.create(tenant=target_tenant, **defaults)

    def test_tenant_scoped_endpoints(self):
        own = self._create_automation()
        other = self._create_automation(tenant=self.other_tenant)

        list_response = self.client.get("/api/v1/automations/")
        self.assertEqual(list_response.status_code, 200)
        returned_ids = {item["id"] for item in list_response.json()}
        self.assertEqual(returned_ids, {str(own.id)})

        own_detail = self.client.get(f"/api/v1/automations/{own.id}/")
        self.assertEqual(own_detail.status_code, 200)

        other_detail = self.client.get(f"/api/v1/automations/{other.id}/")
        self.assertEqual(other_detail.status_code, 404)

        other_patch = self.client.patch(
            f"/api/v1/automations/{other.id}/",
            data={"timezone": "America/Los_Angeles"},
            format="json",
        )
        self.assertEqual(other_patch.status_code, 404)

    def test_pause_resume_semantics(self):
        automation = self._create_automation(next_run_at=timezone.now() - timedelta(hours=3))

        pause_response = self.client.post(f"/api/v1/automations/{automation.id}/pause/")
        self.assertEqual(pause_response.status_code, 200)
        automation.refresh_from_db()
        self.assertEqual(automation.status, Automation.Status.PAUSED)

        previous_next_run = automation.next_run_at
        resume_response = self.client.post(f"/api/v1/automations/{automation.id}/resume/")
        self.assertEqual(resume_response.status_code, 200)
        automation.refresh_from_db()
        self.assertEqual(automation.status, Automation.Status.ACTIVE)
        self.assertGreater(automation.next_run_at, previous_next_run)

    @patch("apps.automations.services.forward_to_openclaw", new_callable=AsyncMock)
    def test_manual_run_creates_run_and_dispatches_once(self, mock_forward):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_fqdn = "oc-automation.internal.azurecontainerapps.io"
        self.tenant.save(update_fields=["status", "container_fqdn", "updated_at"])
        mock_forward.return_value = {"ok": True}

        automation = self._create_automation()

        response = self.client.post(f"/api/v1/automations/{automation.id}/run/")
        self.assertEqual(response.status_code, 201)

        body = response.json()
        self.assertEqual(body["trigger_source"], AutomationRun.TriggerSource.MANUAL)
        self.assertEqual(body["status"], AutomationRun.Status.SUCCEEDED)
        self.assertEqual(AutomationRun.objects.filter(automation=automation).count(), 1)
        mock_forward.assert_awaited_once()

    def test_runs_endpoints_are_tenant_scoped(self):
        own_automation = self._create_automation()
        other_automation = self._create_automation(tenant=self.other_tenant)

        own_run = AutomationRun.objects.create(
            automation=own_automation,
            tenant=self.tenant,
            status=AutomationRun.Status.SUCCEEDED,
            trigger_source=AutomationRun.TriggerSource.SCHEDULE,
            scheduled_for=timezone.now(),
            started_at=timezone.now(),
            finished_at=timezone.now(),
            idempotency_key=f"test:{uuid.uuid4()}",
        )
        AutomationRun.objects.create(
            automation=other_automation,
            tenant=self.other_tenant,
            status=AutomationRun.Status.SUCCEEDED,
            trigger_source=AutomationRun.TriggerSource.SCHEDULE,
            scheduled_for=timezone.now(),
            started_at=timezone.now(),
            finished_at=timezone.now(),
            idempotency_key=f"test:{uuid.uuid4()}",
        )

        runs_response = self.client.get("/api/v1/automations/runs/")
        self.assertEqual(runs_response.status_code, 200)
        runs_body = runs_response.json()
        self.assertEqual(len(runs_body["results"]), 1)
        self.assertEqual(runs_body["results"][0]["id"], str(own_run.id))

        scoped_runs_response = self.client.get(f"/api/v1/automations/{own_automation.id}/runs/")
        self.assertEqual(scoped_runs_response.status_code, 200)
        scoped_body = scoped_runs_response.json()
        self.assertEqual(len(scoped_body["results"]), 1)

    def test_create_validation_rejects_invalid_timezone(self):
        response = self.client.post(
            "/api/v1/automations/",
            data={
                "kind": Automation.Kind.DAILY_BRIEF,
                "status": Automation.Status.ACTIVE,
                "timezone": "Not/A_Real_Timezone",
                "schedule_type": Automation.ScheduleType.DAILY,
                "schedule_time": "09:00:00",
                "schedule_days": [],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("timezone", response.json())


class AutomationSchedulerTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Scheduler User", telegram_chat_id=880101)

    def _create_automation(self, **overrides) -> Automation:
        defaults = {
            "tenant": self.tenant,
            "kind": Automation.Kind.DAILY_BRIEF,
            "status": Automation.Status.ACTIVE,
            "timezone": "UTC",
            "schedule_type": Automation.ScheduleType.DAILY,
            "schedule_time": time(9, 0),
            "schedule_days": [],
            "next_run_at": timezone.now() + timedelta(hours=1),
        }
        defaults.update(overrides)
        return Automation.objects.create(**defaults)

    @patch("apps.automations.scheduler.execute_automation")
    def test_scheduled_runner_processes_due_automations_only(self, mock_execute):
        now = timezone.now()
        due = self._create_automation(next_run_at=now - timedelta(minutes=1))
        self._create_automation(next_run_at=now + timedelta(minutes=30))
        mock_execute.return_value = SimpleNamespace(status=AutomationRun.Status.SUCCEEDED)

        summary = run_due_automations(now=now)

        self.assertEqual(summary["due_count"], 1)
        self.assertEqual(summary["processed_count"], 1)
        self.assertEqual(summary["succeeded"], 1)
        mock_execute.assert_called_once()
        self.assertEqual(mock_execute.call_args.kwargs["automation"].id, due.id)


class AutomationExecutionPolicyTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Policy User", telegram_chat_id=880201)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_fqdn = "oc-policy.internal.azurecontainerapps.io"
        self.tenant.save(update_fields=["status", "container_fqdn", "updated_at"])

    def _create_automation(self, **overrides) -> Automation:
        defaults = {
            "tenant": self.tenant,
            "kind": Automation.Kind.DAILY_BRIEF,
            "status": Automation.Status.ACTIVE,
            "timezone": "UTC",
            "schedule_type": Automation.ScheduleType.DAILY,
            "schedule_time": time(9, 0),
            "schedule_days": [],
            "next_run_at": timezone.now() + timedelta(minutes=5),
        }
        defaults.update(overrides)
        return Automation.objects.create(**defaults)

    def test_manual_run_enforces_min_interval(self):
        automation = self._create_automation(last_run_at=timezone.now() - timedelta(minutes=30))

        with self.assertRaises(AutomationLimitError):
            execute_automation(
                automation=automation,
                trigger_source=AutomationRun.TriggerSource.MANUAL,
            )

    def test_scheduled_run_skips_when_min_interval_not_met(self):
        scheduled_for = timezone.now() - timedelta(minutes=1)
        automation = self._create_automation(
            last_run_at=timezone.now() - timedelta(minutes=30),
            next_run_at=scheduled_for,
        )

        run = execute_automation(
            automation=automation,
            trigger_source=AutomationRun.TriggerSource.SCHEDULE,
            scheduled_for=scheduled_for,
        )

        automation.refresh_from_db()
        self.assertEqual(run.status, AutomationRun.Status.SKIPPED)
        self.assertIn("min interval", run.error_message)
        self.assertGreater(automation.next_run_at, scheduled_for)

    def test_manual_run_enforces_daily_cap(self):
        automation = self._create_automation()
        now = timezone.now()
        for _ in range(12):
            AutomationRun.objects.create(
                automation=automation,
                tenant=self.tenant,
                status=AutomationRun.Status.SUCCEEDED,
                trigger_source=AutomationRun.TriggerSource.SCHEDULE,
                scheduled_for=now,
                started_at=now,
                finished_at=now,
                idempotency_key=f"cap:{uuid.uuid4()}",
            )

        with self.assertRaises(AutomationLimitError):
            execute_automation(
                automation=automation,
                trigger_source=AutomationRun.TriggerSource.MANUAL,
            )

    @patch("apps.automations.services.forward_to_openclaw", new_callable=AsyncMock)
    def test_failed_dispatch_marks_run_failed_and_advances_schedule(self, mock_forward):
        mock_forward.return_value = None
        scheduled_for = timezone.now() - timedelta(minutes=1)
        automation = self._create_automation(next_run_at=scheduled_for)

        run = execute_automation(
            automation=automation,
            trigger_source=AutomationRun.TriggerSource.SCHEDULE,
            scheduled_for=scheduled_for,
        )

        automation.refresh_from_db()
        self.assertEqual(run.status, AutomationRun.Status.FAILED)
        self.assertIn("no response", run.error_message.lower())
        self.assertGreater(automation.next_run_at, scheduled_for)
        self.assertIsNone(automation.last_run_at)

    def test_synthetic_telegram_payload_shape(self):
        automation = self._create_automation()
        run_id = uuid.uuid4()

        payload = _build_synthetic_telegram_update(automation, run_id)

        self.assertIn("update_id", payload)
        self.assertIn("message", payload)
        self.assertEqual(payload["message"]["chat"]["id"], self.tenant.user.telegram_chat_id)
        self.assertIn(f"run_id={run_id}", payload["message"]["text"])
        self.assertIn("AUTOMATION:daily_brief", payload["message"]["text"])
