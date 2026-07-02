"""Tests for the per-pillar snapshotters (Fuel / Core / Journal) and the
``snapshot_pillars_weekly_task`` cron entrypoint.

Compute-function tests pass an explicit ``today`` for determinism; the task
tests exercise enablement gating + ISO-week idempotency with real-today data.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import MeditationSession, MeditationStatus
from apps.fuel.models import (
    BodyWeightLog,
    PlanStatus,
    Workout,
    WorkoutCategory,
    WorkoutPlan,
    WorkoutStatus,
)
from apps.insights.models import PillarSnapshot
from apps.insights.pillars import Pillar
from apps.insights.snapshots import (
    compute_core_snapshot,
    compute_fuel_snapshot,
    compute_journal_snapshot,
)
from apps.insights.tasks import snapshot_pillars_weekly_task
from apps.journal.models import Goal, JournalEntry, Task
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

TODAY = date(2026, 5, 20)  # Wednesday, ISO week 21


def _active_tenant(*, chat_id: int, fuel=False, core=False) -> Tenant:
    tenant = create_tenant(display_name=f"Snap-{chat_id}", telegram_chat_id=chat_id)
    Tenant.objects.filter(pk=tenant.pk).update(
        status=Tenant.Status.ACTIVE,
        fuel_enabled=fuel,
        core_enabled=core,
    )
    tenant.refresh_from_db()
    return tenant


def _workout(tenant, *, on: date, status=WorkoutStatus.DONE, minutes=None, plan=None) -> Workout:
    return Workout.objects.create(
        tenant=tenant,
        date=on,
        status=status,
        category=WorkoutCategory.STRENGTH,
        activity="Push",
        duration_minutes=minutes,
        plan=plan,
    )


def _session(tenant, *, on: date, status=MeditationStatus.READY) -> MeditationSession:
    return MeditationSession.objects.create(tenant=tenant, date=on, status=status)


def _entry(tenant, *, on: date) -> JournalEntry:
    return JournalEntry.objects.create(
        tenant=tenant, date=on, mood="ok", energy=JournalEntry.Energy.MEDIUM, raw_text="..."
    )


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class FuelSnapshotTests(TestCase):
    def setUp(self):
        self.tenant = _active_tenant(chat_id=902001, fuel=True)

    def test_volume_and_body_weight_delta(self):
        _workout(self.tenant, on=TODAY, minutes=60)
        _workout(self.tenant, on=TODAY - timedelta(days=4), minutes=30)  # 7d
        _workout(self.tenant, on=TODAY - timedelta(days=10), minutes=45)  # 28d only
        _workout(self.tenant, on=TODAY - timedelta(days=40), minutes=99)  # out of window
        _workout(self.tenant, on=TODAY, status=WorkoutStatus.PLANNED, minutes=15)  # not DONE

        BodyWeightLog.objects.create(tenant=self.tenant, date=TODAY - timedelta(days=26), weight_kg="80.00")
        BodyWeightLog.objects.create(tenant=self.tenant, date=TODAY, weight_kg="78.50")

        snap = compute_fuel_snapshot(self.tenant, today=TODAY)
        totals = snap["totals"]
        self.assertEqual(totals["workouts_7d"], 2)
        self.assertEqual(totals["minutes_7d"], 90)
        self.assertEqual(totals["workouts_28d"], 3)
        self.assertEqual(totals["minutes_28d"], 135)
        self.assertEqual(totals["body_weight_kg"], "78.50")
        self.assertEqual(totals["body_weight_delta_28d"], "-1.50")
        self.assertIsNone(snap["active_plan"])

    def test_active_plan_adherence(self):
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Strength Builder",
            status=PlanStatus.ACTIVE,
            start_date=TODAY - timedelta(days=7),
            weeks=4,
            days_per_week=3,
        )
        _workout(self.tenant, on=TODAY, status=WorkoutStatus.DONE, plan=plan)
        _workout(self.tenant, on=TODAY - timedelta(days=1), status=WorkoutStatus.PLANNED, plan=plan)
        _workout(self.tenant, on=TODAY - timedelta(days=2), status=WorkoutStatus.REST, plan=plan)

        snap = compute_fuel_snapshot(self.tenant, today=TODAY)
        plan_block = snap["active_plan"]
        self.assertEqual(plan_block["name"], "Strength Builder")
        self.assertEqual(plan_block["scheduled_this_week"], 2)  # REST excluded
        self.assertEqual(plan_block["completed_this_week"], 1)
        self.assertEqual(plan_block["adherence"], 0.5)

    def test_empty_fuel_snapshot(self):
        snap = compute_fuel_snapshot(self.tenant, today=TODAY)
        self.assertEqual(snap["totals"]["workouts_28d"], 0)
        self.assertIsNone(snap["totals"]["body_weight_kg"])
        self.assertIsNone(snap["totals"]["body_weight_delta_28d"])


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class CoreSnapshotTests(TestCase):
    def setUp(self):
        self.tenant = _active_tenant(chat_id=902010, core=True)

    def test_sessions_and_streak_ending_today(self):
        for d in (TODAY, TODAY - timedelta(days=1), TODAY - timedelta(days=2)):
            _session(self.tenant, on=d)
        _session(self.tenant, on=TODAY - timedelta(days=10))  # 28d, breaks streak
        _session(self.tenant, on=TODAY, status=MeditationStatus.PENDING)  # not a completed sit

        snap = compute_core_snapshot(self.tenant, today=TODAY)
        totals = snap["totals"]
        self.assertEqual(totals["sessions_7d"], 3)
        self.assertEqual(totals["sessions_28d"], 4)
        self.assertEqual(totals["practice_streak_days"], 3)
        self.assertEqual(snap["last_session_date"], TODAY.isoformat())

    def test_streak_counts_from_yesterday_when_today_empty(self):
        _session(self.tenant, on=TODAY - timedelta(days=1))
        _session(self.tenant, on=TODAY - timedelta(days=2))
        snap = compute_core_snapshot(self.tenant, today=TODAY)
        self.assertEqual(snap["totals"]["practice_streak_days"], 2)

    def test_no_streak_when_gap(self):
        _session(self.tenant, on=TODAY - timedelta(days=3))
        snap = compute_core_snapshot(self.tenant, today=TODAY)
        self.assertEqual(snap["totals"]["practice_streak_days"], 0)


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class JournalSnapshotTests(TestCase):
    def setUp(self):
        self.tenant = _active_tenant(chat_id=902020)

    def test_entries_goals_and_tasks(self):
        _entry(self.tenant, on=TODAY)
        _entry(self.tenant, on=TODAY - timedelta(days=5))  # 7d
        _entry(self.tenant, on=TODAY - timedelta(days=12))  # 28d only
        _entry(self.tenant, on=TODAY - timedelta(days=40))  # out of window

        Goal.objects.create(tenant=self.tenant, title="Save more", status=Goal.Status.ACTIVE)
        Goal.objects.create(tenant=self.tenant, title="Read 12 books", status=Goal.Status.ACTIVE)
        Goal.objects.create(tenant=self.tenant, title="Old goal", status=Goal.Status.ACHIEVED)

        yesterday_noon = timezone.make_aware(
            timezone.datetime.combine(TODAY - timedelta(days=1), timezone.datetime.min.time())
        ) + timedelta(hours=12)
        Task.objects.create(
            tenant=self.tenant, title="done recently", status=Task.Status.DONE, completed_at=yesterday_noon
        )
        old_done = timezone.make_aware(
            timezone.datetime.combine(TODAY - timedelta(days=15), timezone.datetime.min.time())
        )
        Task.objects.create(tenant=self.tenant, title="done long ago", status=Task.Status.DONE, completed_at=old_done)
        Task.objects.create(tenant=self.tenant, title="open task", status=Task.Status.OPEN)
        Task.objects.create(tenant=self.tenant, title="wip task", status=Task.Status.IN_PROGRESS)

        snap = compute_journal_snapshot(self.tenant, today=TODAY)
        totals = snap["totals"]
        self.assertEqual(totals["entries_7d"], 2)
        self.assertEqual(totals["entries_28d"], 3)
        self.assertEqual(totals["active_goals"], 2)
        self.assertEqual(totals["tasks_completed_7d"], 1)
        self.assertEqual(totals["tasks_open"], 2)
        self.assertEqual(snap["last_entry_date"], TODAY.isoformat())


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class SnapshotPillarsWeeklyTaskTests(TestCase):
    def _seed_today_activity(self, tenant):
        today = timezone.now().date()
        _workout(tenant, on=today, minutes=30)
        _session(tenant, on=today)
        _entry(tenant, on=today)

    def test_writes_enabled_pillars(self):
        tenant = _active_tenant(chat_id=902030, fuel=True, core=True)
        self._seed_today_activity(tenant)

        counts = snapshot_pillars_weekly_task()
        self.assertEqual(counts["fuel_written"], 1)
        self.assertEqual(counts["core_written"], 1)
        self.assertEqual(counts["journal_written"], 1)

        for pillar in (Pillar.FUEL.value, Pillar.CORE.value, Pillar.JOURNAL.value):
            self.assertTrue(
                PillarSnapshot.objects.filter(
                    tenant=tenant, pillar=pillar, granularity=PillarSnapshot.Granularity.WEEKLY
                ).exists()
            )

    def test_disabled_pillars_are_skipped_but_journal_always_writes(self):
        tenant = _active_tenant(chat_id=902031, fuel=False, core=False)
        self._seed_today_activity(tenant)

        counts = snapshot_pillars_weekly_task()
        self.assertEqual(counts["fuel_written"], 0)
        self.assertEqual(counts["core_written"], 0)
        self.assertEqual(counts["journal_written"], 1)
        self.assertFalse(PillarSnapshot.objects.filter(tenant=tenant, pillar=Pillar.FUEL.value).exists())
        self.assertFalse(PillarSnapshot.objects.filter(tenant=tenant, pillar=Pillar.CORE.value).exists())
        self.assertTrue(PillarSnapshot.objects.filter(tenant=tenant, pillar=Pillar.JOURNAL.value).exists())

    def test_hibernated_tenant_gets_nothing(self):
        tenant = _active_tenant(chat_id=902032, fuel=True, core=True)
        Tenant.objects.filter(pk=tenant.pk).update(hibernated_at=timezone.now())

        counts = snapshot_pillars_weekly_task()
        self.assertEqual(counts["fuel_written"], 0)
        self.assertEqual(counts["core_written"], 0)
        self.assertEqual(counts["journal_written"], 0)
        self.assertFalse(PillarSnapshot.objects.filter(tenant=tenant).exists())

    def test_idempotent_within_iso_week(self):
        tenant = _active_tenant(chat_id=902033, fuel=True, core=True)
        self._seed_today_activity(tenant)

        snapshot_pillars_weekly_task()
        counts2 = snapshot_pillars_weekly_task()
        self.assertEqual(counts2["fuel_written"], 0)
        self.assertEqual(counts2["fuel_skipped"], 1)
        self.assertEqual(counts2["journal_skipped"], 1)
        # No duplicate rows.
        self.assertEqual(PillarSnapshot.objects.filter(tenant=tenant, pillar=Pillar.JOURNAL.value).count(), 1)
