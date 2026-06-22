"""Adversarial audit tests — cluster A01 (FA-0026).

These tests cover the gap in the FA-0026 fix: the SKIPPED branch used a bare
AutomationRun.objects.create() with a window-deterministic idempotency key,
which raised IntegrityError when a stranded RUNNING row already occupied that
key.  The scheduler swallowed the IntegrityError as errors+=1 and never
advanced next_run_at, causing the automation to be re-selected and crash every
tick — an unrecoverable loop.

The fix replaces the bare create() with get_or_create() so the SKIPPED branch
is idempotent under key collision.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone

from apps.tenants.services import create_tenant

from .models import Automation, AutomationRun
from .scheduler import run_due_automations
from .services import STALE_RUNNING_THRESHOLD, _build_idempotency_key, execute_automation


def _make_automation(tenant, *, next_run_at=None, last_run_at=None) -> Automation:
    return Automation.objects.create(
        tenant=tenant,
        kind=Automation.Kind.DAILY_BRIEF,
        status=Automation.Status.ACTIVE,
        timezone="UTC",
        schedule_type=Automation.ScheduleType.DAILY,
        schedule_time=time(9, 0),
        schedule_days=[],
        next_run_at=next_run_at or timezone.now() - timedelta(minutes=1),
        last_run_at=last_run_at,
    )


class SkippedBranchIdempotencyTest(TestCase):
    """FA-0026: SKIPPED branch must not raise IntegrityError on key collision."""

    def setUp(self):
        self.tenant = create_tenant(display_name="A01 User", telegram_chat_id=991001)

    def _plant_stranded_running_row(self, automation: Automation, scheduled_for: datetime) -> AutomationRun:
        """Insert a stranded RUNNING row that occupies the window-deterministic key."""
        key = _build_idempotency_key(automation, AutomationRun.TriggerSource.SCHEDULE, scheduled_for)
        stale_started_at = scheduled_for - STALE_RUNNING_THRESHOLD - timedelta(minutes=5)
        return AutomationRun.objects.create(
            automation=automation,
            tenant=automation.tenant,
            status=AutomationRun.Status.RUNNING,
            trigger_source=AutomationRun.TriggerSource.SCHEDULE,
            scheduled_for=scheduled_for,
            started_at=stale_started_at,
            idempotency_key=key,
            input_payload={},
        )

    def test_skipped_branch_does_not_raise_on_key_collision(self):
        """get_or_create in SKIPPED branch prevents IntegrityError when key already exists."""
        # Set last_run_at to very recently so MIN_RUN_INTERVAL trips and the
        # SKIPPED branch is taken.
        recent = timezone.now() - timedelta(minutes=5)
        pinned_window = timezone.now() - timedelta(hours=3)
        automation = _make_automation(
            self.tenant,
            next_run_at=pinned_window,
            last_run_at=recent,
        )

        # Plant a stranded RUNNING row for the same window.
        stranded = self._plant_stranded_running_row(automation, pinned_window)

        # This must NOT raise; the old bare create() would have raised
        # IntegrityError here.
        try:
            run = execute_automation(
                automation=automation,
                trigger_source=AutomationRun.TriggerSource.SCHEDULE,
                scheduled_for=pinned_window,
            )
        except Exception as exc:  # pragma: no cover
            self.fail(f"execute_automation raised unexpectedly: {exc}")

        # The returned run is the pre-existing row (get returned the existing row).
        self.assertEqual(run.id, stranded.id)

        # next_run_at must have been advanced so the scheduler can escape the loop.
        automation.refresh_from_db()
        self.assertGreater(automation.next_run_at, pinned_window)

    def test_skipped_branch_advances_next_run_at_on_fresh_skipped_row(self):
        """SKIPPED branch (new row) still advances next_run_at."""
        recent = timezone.now() - timedelta(minutes=5)
        pinned_window = timezone.now() - timedelta(hours=3)
        automation = _make_automation(
            self.tenant,
            next_run_at=pinned_window,
            last_run_at=recent,
        )

        run = execute_automation(
            automation=automation,
            trigger_source=AutomationRun.TriggerSource.SCHEDULE,
            scheduled_for=pinned_window,
        )

        self.assertEqual(run.status, AutomationRun.Status.SKIPPED)
        automation.refresh_from_db()
        self.assertGreater(automation.next_run_at, pinned_window)

    def test_scheduler_does_not_loop_on_key_collision(self):
        """run_due_automations must not log errors when SKIPPED key is already occupied."""
        recent = timezone.now() - timedelta(minutes=5)
        pinned_window = timezone.now() - timedelta(hours=3)
        automation = _make_automation(
            self.tenant,
            next_run_at=pinned_window,
            last_run_at=recent,
        )

        # Plant stranded row occupying the deterministic key.
        self._plant_stranded_running_row(automation, pinned_window)

        summary = run_due_automations()

        # Before the fix this would have been errors=1, skipped=0;
        # after the fix the collision is handled gracefully.
        self.assertEqual(
            summary["errors"],
            0,
            f"Scheduler reported errors; full summary: {summary}",
        )
        # The automation was processed (skipped or otherwise) without an unhandled exception.
        self.assertEqual(summary["processed_count"], 1)

    def test_negative_min_run_interval_delta_trips_skipped_branch(self):
        """A manual run that sets last_run_at > next_run_at leaves a negative delta
        which is < MIN_RUN_INTERVAL and so trips the SKIPPED path on the next tick."""
        pinned_window = timezone.now() - timedelta(hours=3)
        # last_run_at set to AFTER pinned_window (simulating a later manual run)
        later_success = pinned_window + timedelta(minutes=30)
        automation = _make_automation(
            self.tenant,
            next_run_at=pinned_window,
            last_run_at=later_success,
        )

        # No stranded row — should create a fresh SKIPPED row without error.
        run = execute_automation(
            automation=automation,
            trigger_source=AutomationRun.TriggerSource.SCHEDULE,
            scheduled_for=pinned_window,
        )

        self.assertEqual(run.status, AutomationRun.Status.SKIPPED)
        # Error message should mention the interval, not an integrity error.
        self.assertIn("min interval", run.error_message)

        # next_run_at must have advanced.
        automation.refresh_from_db()
        self.assertGreater(automation.next_run_at, pinned_window)
