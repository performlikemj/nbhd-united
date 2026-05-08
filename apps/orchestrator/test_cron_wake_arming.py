"""Tests for cron-aware wake arming — lead time and already-awake re-chain.

Two regression guards:

1. ``_CRON_WAKE_LEAD_SECONDS`` is large enough to cover the worst-case
   cold-start (image refresh + plugin runtime deps materialization can
   take ~3 min). Previous 120s was tight and a slow start could miss
   the cron entirely.

2. ``wake_for_cron_task`` re-arms the next cron wake when it fires and
   the tenant is already awake. Previously it returned early and left
   the wake chain to be re-armed only by the next idle-hibernation
   cycle — fragile if that fails or is delayed.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.orchestrator import hibernation
from apps.orchestrator.hibernation import (
    _CRON_WAKE_LEAD_SECONDS,
    _schedule_next_cron_wake,
    wake_for_cron_task,
)
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class CronWakeLeadTimeTests(TestCase):
    """The lead time floor must cover real-world cold-start latency."""

    def test_lead_seconds_constant_covers_worst_case_cold_start(self):
        # Image refresh on wake (PR #384) + plugin-runtime-deps install on
        # EmptyDir (PR #387) + first plugin spawn can run past 3 min on a
        # cold revision. 240s leaves a ~60s buffer before the cron fires.
        self.assertGreaterEqual(_CRON_WAKE_LEAD_SECONDS, 180)


class ScheduleNextCronWakeTests(TestCase):
    """``_schedule_next_cron_wake`` honours the lead-time setting.

    These tests patch ``_find_earliest_next_run`` directly so they don't
    depend on the gateway-shape fix (PR #476) and stay focused on the
    delay-arithmetic + lead-time floor we're asserting here.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="LeadTime", telegram_chat_id=950100)
        self.tenant.container_id = "oc-leadtime"
        self.tenant.save(update_fields=["container_id"])

    @patch("apps.cron.publish.publish_task")
    @patch.object(hibernation, "_find_earliest_next_run")
    def test_far_future_cron_uses_full_lead(self, mock_earliest, mock_publish):
        # Cron fires in 30 min → wake should fire (1800 - lead) seconds out.
        now_ms = int(timezone.now().timestamp() * 1000)
        mock_earliest.return_value = now_ms + 1_800_000

        _schedule_next_cron_wake(self.tenant, [{"placeholder": True}])

        mock_publish.assert_called_once()
        kwargs = mock_publish.call_args.kwargs
        expected_delay = 1800 - _CRON_WAKE_LEAD_SECONDS
        self.assertAlmostEqual(kwargs["delay_seconds"], expected_delay, delta=2)

    @patch("apps.cron.publish.publish_task")
    @patch.object(hibernation, "_find_earliest_next_run")
    def test_near_future_cron_floors_at_60s(self, mock_earliest, mock_publish):
        # Cron fires in 90s, well inside the lead-time buffer → floor to 60.
        now_ms = int(timezone.now().timestamp() * 1000)
        mock_earliest.return_value = now_ms + 90_000

        _schedule_next_cron_wake(self.tenant, [{"placeholder": True}])

        mock_publish.assert_called_once()
        self.assertEqual(mock_publish.call_args.kwargs["delay_seconds"], 60)


class WakeForCronAlreadyAwakeTests(TestCase):
    """When wake_for_cron fires on an already-awake tenant, re-arm the next
    cron wake instead of bailing silently — defensive against the case where
    the next idle-hibernation fails or doesn't run before the next cron."""

    def setUp(self):
        self.tenant = create_tenant(display_name="AlreadyAwake", telegram_chat_id=950101)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-already-awake"
        self.tenant.container_fqdn = "oc-already-awake.internal"
        self.tenant.hibernated_at = None  # awake
        self.tenant.save()

    def test_already_awake_rearms_next_cron_wake(self):
        captured_jobs = [
            {
                "name": "Evening Check-in",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "0 21 * * *", "tz": "UTC"},
                "state": {"nextRunAtMs": int(timezone.now().timestamp() * 1000) + 7_200_000},
            }
        ]

        with (
            patch.object(
                hibernation,
                "_capture_tenant_cron_schedules",
                return_value=captured_jobs,
            ) as mock_capture,
            patch.object(hibernation, "_schedule_next_cron_wake") as mock_schedule,
        ):
            result = wake_for_cron_task(str(self.tenant.id))

        self.assertEqual(result["status"], "already_awake")
        # Re-armed: capture and schedule both called.
        mock_capture.assert_called_once()
        mock_schedule.assert_called_once()
        scheduled_jobs = mock_schedule.call_args.args[1]
        self.assertEqual(scheduled_jobs, captured_jobs)

    def test_already_awake_rearm_failure_is_non_fatal(self):
        """If capture or schedule fails, return already_awake anyway —
        the missed-wake risk is preferred to crashing the QStash callback,
        which would just retry and re-encounter the same failure."""
        with (
            patch.object(
                hibernation,
                "_capture_tenant_cron_schedules",
                side_effect=RuntimeError("simulated capture failure"),
            ),
        ):
            result = wake_for_cron_task(str(self.tenant.id))

        self.assertEqual(result["status"], "already_awake")

    def test_hibernated_path_unchanged(self):
        """Sanity check: when the tenant IS hibernated, the wake path runs
        as before — capture-and-schedule is NOT invoked here (that work
        belongs to the next idle-hibernation, not the wake itself)."""
        self.tenant.hibernated_at = timezone.now()
        self.tenant.save(update_fields=["hibernated_at"])

        with (
            patch.object(hibernation, "wake_hibernated_tenant", return_value=True) as mock_wake,
            patch.object(hibernation, "_capture_tenant_cron_schedules") as mock_capture,
            patch.object(hibernation, "_schedule_next_cron_wake") as mock_schedule,
            patch("apps.cron.publish.publish_task"),
        ):
            result = wake_for_cron_task(str(self.tenant.id))

        self.assertEqual(result["status"], "woken_for_cron")
        mock_wake.assert_called_once()
        # Re-arming is the awake-path fix; the hibernated path didn't have
        # the bug because idle-hibernation arms the next wake.
        mock_capture.assert_not_called()
        mock_schedule.assert_not_called()
