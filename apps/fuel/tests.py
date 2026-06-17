"""Fuel module tests — services, models, consumer views, runtime views."""

from __future__ import annotations

from datetime import UTC, date, timedelta
from decimal import Decimal
from unittest import TestCase as UnitTestCase
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.tenants.services import create_tenant

from .models import BodyWeightLog, FuelProfile, PlanSlot, Workout, WorkoutPlan
from .services import est_1rm

# ═════════════════════════════════════════════════════════════════════
# 1. Service Tests (pure math, no DB)
# ═════════════════════════════════════════════════════════════════════


class Est1RMTests(UnitTestCase):
    """Test the Epley 1RM estimation formula."""

    def test_single_rep_returns_weight(self):
        self.assertEqual(est_1rm(100, 1), 100.0)

    def test_standard_calculation(self):
        # 100kg x 5 reps → 100 * (1 + 5/30) = 116.7
        self.assertAlmostEqual(est_1rm(100, 5), 116.7, places=1)

    def test_ten_reps(self):
        # 80kg x 10 reps → 80 * (1 + 10/30) = 106.7
        self.assertAlmostEqual(est_1rm(80, 10), 106.7, places=1)

    def test_zero_weight(self):
        self.assertEqual(est_1rm(0, 8), 0.0)

    def test_zero_reps(self):
        self.assertEqual(est_1rm(100, 0), 0.0)

    def test_negative_reps(self):
        self.assertEqual(est_1rm(100, -1), 0.0)


# ═════════════════════════════════════════════════════════════════════
# 2. Model Tests
# ═════════════════════════════════════════════════════════════════════


class WorkoutModelTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Fuel Test", telegram_chat_id=800001)

    def test_create_strength_workout(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push — Chest & Shoulders",
            duration_minutes=60,
            rpe=7,
            detail_json={
                "exercises": [
                    {"name": "Bench Press", "sets": [{"reps": 8, "weight": 72.5}]},
                ]
            },
        )
        self.assertEqual(str(w.date), "2026-04-21")
        self.assertEqual(w.category, "strength")
        self.assertEqual(w.status, "done")

    def test_create_cardio_workout(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 20),
            category="cardio",
            activity="Zone 2 run",
            duration_minutes=42,
            detail_json={"distance_km": 7.2, "pace": "5:50", "avg_hr": 142},
        )
        self.assertEqual(w.detail_json["distance_km"], 7.2)

    def test_planned_status(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 25),
            status="planned",
            category="strength",
            activity="Leg Day",
        )
        self.assertEqual(w.status, "planned")

    def test_ordering(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 19),
            category="cardio",
            activity="Run",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        workouts = list(Workout.objects.filter(tenant=self.tenant))
        self.assertEqual(workouts[0].date, date(2026, 4, 21))
        self.assertEqual(workouts[1].date, date(2026, 4, 19))


class PlanSlotModelTests(TestCase):
    """Phase 1 — stable plan-slot identity for the plan reconciler.

    The slot owns the `(plan, week_index, weekday)` intent so a plan-regen
    can mutate slots in place without tombstoning workout uuids.
    """

    def setUp(self):
        from django.utils import timezone

        self.tenant = create_tenant(display_name="Slot Test", telegram_chat_id=800050)
        self.plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Reconciler Smoke",
            start_date=date(2026, 6, 1),
            weeks=4,
            days_per_week=3,
            schedule_json={"0": {"category": "strength", "activity": "Push"}},
        )
        self._tz_now = timezone.now

    def test_slot_create_and_natural_key(self):
        slot = PlanSlot.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            week_index=0,
            weekday=0,
        )
        self.assertEqual(slot.plan_id, self.plan.id)
        self.assertEqual(slot.week_index, 0)
        self.assertEqual(slot.weekday, 0)
        self.assertIsNone(slot.archived_at)

    def test_active_uniqueness_enforced(self):
        from django.db import IntegrityError, transaction

        PlanSlot.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            week_index=1,
            weekday=2,
        )
        # Active duplicate must fail.
        with self.assertRaises(IntegrityError), transaction.atomic():
            PlanSlot.objects.create(
                tenant=self.tenant,
                plan=self.plan,
                week_index=1,
                weekday=2,
            )

    def test_archived_slot_allows_recreation_at_same_key(self):
        # When the assistant removes a slot from schedule_json but a workout
        # row still references it, we soft-archive. A future regen that
        # re-adds that (week, weekday) must be allowed to create a fresh slot.
        original = PlanSlot.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            week_index=2,
            weekday=4,
        )
        original.archived_at = self._tz_now()
        original.save(update_fields=["archived_at"])
        # Same natural key should succeed because the original is archived.
        replacement = PlanSlot.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            week_index=2,
            weekday=4,
        )
        self.assertNotEqual(original.id, replacement.id)
        self.assertIsNone(replacement.archived_at)

    def test_workout_slot_set_null_on_slot_delete(self):
        slot = PlanSlot.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            week_index=0,
            weekday=3,
        )
        w = Workout.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            slot=slot,
            date=date(2026, 6, 4),
            status="planned",
            category="strength",
            activity="Push",
        )
        slot.delete()
        w.refresh_from_db()
        self.assertIsNone(w.slot)
        # Workout itself must survive — losing the slot must not tombstone the row.
        self.assertEqual(w.status, "planned")

    def test_workout_new_fields_default_to_unlocked_unedited(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 6, 5),
            category="strength",
            activity="Push",
        )
        self.assertEqual(w.version, 0)
        self.assertIsNone(w.edit_lock_until)
        self.assertEqual(w.edit_lock_owner, "")
        self.assertIsNone(w.last_edited_by_user_at)


class BackfillPlanSlotsTests(TestCase):
    """Phase 2 — the backfill helper materializes slots and back-links workouts.

    Uses the live ORM rather than re-running the migration so we can assert
    against actual fixture data (the migration ran against an empty DB).
    """

    def setUp(self):
        from apps.fuel.services import backfill_plan_slots as _bf

        self._bf = _bf
        self.tenant = create_tenant(display_name="Backfill Test", telegram_chat_id=800060)
        # 2026-06-01 is a Monday — keeps the (weekday=0) math obvious.
        self.plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Backfill Smoke",
            start_date=date(2026, 6, 1),
            weeks=2,
            days_per_week=2,
            schedule_json={
                "0": {"category": "strength", "activity": "Push Day"},
                "3": {"category": "strength", "activity": "Pull Day"},
            },
        )

    def _run(self):
        return self._bf(WorkoutPlan, PlanSlot, Workout)

    def test_backfill_creates_slots_per_week_x_weekday(self):
        stats = self._run()
        # 2 weeks × 2 weekdays = 4 slots.
        self.assertEqual(stats["slots_created"], 4)
        self.assertEqual(stats["plans_skipped"], 0)
        self.assertEqual(PlanSlot.objects.filter(plan=self.plan).count(), 4)
        self.assertEqual(
            set(PlanSlot.objects.values_list("week_index", "weekday")),
            {(0, 0), (0, 3), (1, 0), (1, 3)},
        )

    def test_backfill_is_idempotent(self):
        self._run()
        stats2 = self._run()
        self.assertEqual(stats2["slots_created"], 0)
        self.assertEqual(PlanSlot.objects.filter(plan=self.plan).count(), 4)

    def test_backfill_links_planned_workouts_matching_template(self):
        w_match = Workout.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            date=date(2026, 6, 1),
            status="planned",
            category="strength",
            activity="Push Day",
        )
        # User-renamed workout — should stay slot=NULL.
        w_user_renamed = Workout.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            date=date(2026, 6, 4),
            status="planned",
            category="strength",
            activity="Custom Pull Variation",
        )
        # Out-of-range workout (5 weeks past start, weeks=2) — should stay slot=NULL.
        w_out_of_range = Workout.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            date=date(2026, 7, 6),
            status="planned",
            category="strength",
            activity="Push Day",
        )

        stats = self._run()
        self.assertEqual(stats["workouts_linked"], 1)
        self.assertEqual(stats["workouts_skipped"], 2)

        w_match.refresh_from_db()
        w_user_renamed.refresh_from_db()
        w_out_of_range.refresh_from_db()
        self.assertIsNotNone(w_match.slot)
        self.assertEqual(w_match.slot.week_index, 0)
        self.assertEqual(w_match.slot.weekday, 0)
        self.assertIsNone(w_user_renamed.slot)
        self.assertIsNone(w_out_of_range.slot)

    def test_backfill_skips_plan_with_empty_schedule(self):
        empty_plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Empty",
            start_date=date(2026, 6, 1),
            weeks=4,
            days_per_week=1,
            schedule_json={},
        )
        stats = self._run()
        self.assertEqual(stats["plans_skipped"], 1)
        self.assertEqual(PlanSlot.objects.filter(plan=empty_plan).count(), 0)

    def test_backfill_doesnt_relink_an_existing_slot(self):
        # Pre-link to a slot that's NOT the one the template would pick.
        existing_slot = PlanSlot.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            week_index=1,
            weekday=3,
        )
        w = Workout.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            slot=existing_slot,
            date=date(2026, 6, 1),
            status="planned",
            category="strength",
            activity="Push Day",
        )
        self._run()
        w.refresh_from_db()
        self.assertEqual(w.slot_id, existing_slot.id)


class ReconcilePlanStateTests(TestCase):
    """Phase 3 — diff + apply for the plan reconciler.

    Every test pins ``today`` so the date-window logic is deterministic
    regardless of when the suite actually runs.
    """

    def setUp(self):
        from apps.fuel.services import apply_reconciliation, reconcile_plan_state

        self._reconcile = reconcile_plan_state
        self._apply = apply_reconciliation
        self.tenant = create_tenant(display_name="Recon Test", telegram_chat_id=800100)
        # 2026-06-01 is a Monday. start_date == today keeps things obvious.
        self.today = date(2026, 6, 1)
        self.plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Reconciler",
            start_date=self.today,
            weeks=2,
            days_per_week=2,
            schedule_json={
                "0": {"category": "strength", "activity": "Push Day", "duration_minutes": 60},
                "3": {"category": "strength", "activity": "Pull Day", "duration_minutes": 50},
            },
        )

    # --- Diff (no writes) -----------------------------------------------

    def test_empty_plan_produces_full_grid_of_new_slots(self):
        rec = self._reconcile(self.plan, self.plan.schedule_json, self.plan.weeks, today=self.today)
        self.assertEqual(len(rec.new_slot_keys), 4)
        self.assertEqual(rec.slots_to_archive, [])
        self.assertEqual(rec.workouts_to_delete, [])
        self.assertEqual(len(rec.workouts_to_create), 4)
        # Workout specs carry the template fields.
        first = next(s for s in rec.workouts_to_create if s.slot_key.week_index == 0 and s.slot_key.weekday == 0)
        self.assertEqual(first.activity, "Push Day")
        self.assertEqual(first.duration_minutes, 60)
        self.assertEqual(first.date, date(2026, 6, 1))

    def test_already_synced_plan_is_noop(self):
        rec = self._reconcile(self.plan, self.plan.schedule_json, self.plan.weeks, today=self.today)
        self._apply(rec, plan=self.plan, tenant=self.tenant)
        # Second reconcile against same desired state == noop.
        rec2 = self._reconcile(self.plan, self.plan.schedule_json, self.plan.weeks, today=self.today)
        self.assertTrue(rec2.is_noop, msg=f"Expected noop, got: {rec2}")
        self.assertEqual(len(rec2.slots_kept), 4)

    def test_extending_weeks_adds_slots(self):
        rec = self._reconcile(self.plan, self.plan.schedule_json, self.plan.weeks, today=self.today)
        self._apply(rec, plan=self.plan, tenant=self.tenant)
        # Extend.
        rec2 = self._reconcile(self.plan, self.plan.schedule_json, weeks=4, today=self.today)
        self.assertEqual(len(rec2.new_slot_keys), 4)
        self.assertEqual(rec2.slots_to_archive, [])
        self.assertEqual(len(rec2.slots_kept), 4)

    def test_shrinking_weeks_archives_slots(self):
        rec = self._reconcile(self.plan, self.plan.schedule_json, weeks=4, today=self.today)
        self._apply(rec, plan=self.plan, tenant=self.tenant)
        # Shrink.
        rec2 = self._reconcile(self.plan, self.plan.schedule_json, weeks=2, today=self.today)
        self.assertEqual(rec2.new_slot_keys, [])
        self.assertEqual(len(rec2.slots_to_archive), 4)
        self.assertEqual(len(rec2.slots_kept), 4)
        # The archived ones are weeks 2 and 3.
        archived_keys = {(s.week_index, s.weekday) for s in rec2.slots_to_archive}
        self.assertEqual(archived_keys, {(2, 0), (2, 3), (3, 0), (3, 3)})

    def test_changing_weekday_archives_and_creates(self):
        rec = self._reconcile(self.plan, self.plan.schedule_json, self.plan.weeks, today=self.today)
        self._apply(rec, plan=self.plan, tenant=self.tenant)
        # Schedule moves Push from Monday to Tuesday.
        new_sched = {
            "1": {"category": "strength", "activity": "Push Day"},
            "3": {"category": "strength", "activity": "Pull Day"},
        }
        rec2 = self._reconcile(self.plan, new_sched, self.plan.weeks, today=self.today)
        new_keys = {(k.week_index, k.weekday) for k in rec2.new_slot_keys}
        archived_keys = {(s.week_index, s.weekday) for s in rec2.slots_to_archive}
        self.assertEqual(new_keys, {(0, 1), (1, 1)})
        self.assertEqual(archived_keys, {(0, 0), (1, 0)})

    def test_past_slots_are_out_of_scope(self):
        # today moves forward by 1 week — week 0 is now in the past.
        future_today = self.today + timedelta(weeks=1)
        # Seed week-0 slots so they exist when we pretend "today" is week 1.
        rec_init = self._reconcile(self.plan, self.plan.schedule_json, self.plan.weeks, today=self.today)
        self._apply(rec_init, plan=self.plan, tenant=self.tenant)
        # Now reconcile with future_today: same desired schedule.
        rec = self._reconcile(self.plan, self.plan.schedule_json, self.plan.weeks, today=future_today)
        # Week 0 slots are past — not in scope, not in any diff bucket.
        archive_keys = {(s.week_index, s.weekday) for s in rec.slots_to_archive}
        kept_keys = {(s.week_index, s.weekday) for s in rec.slots_kept}
        self.assertNotIn((0, 0), archive_keys)
        self.assertNotIn((0, 3), archive_keys)
        self.assertNotIn((0, 0), kept_keys)
        # Week 1 slots are kept.
        self.assertIn((1, 0), kept_keys)
        self.assertIn((1, 3), kept_keys)

    # --- Apply (writes) -------------------------------------------------

    def test_apply_creates_slots_and_workouts(self):
        rec = self._reconcile(self.plan, self.plan.schedule_json, self.plan.weeks, today=self.today)
        counts = self._apply(rec, plan=self.plan, tenant=self.tenant)
        self.assertEqual(counts["slots_created"], 4)
        self.assertEqual(counts["workouts_created"], 4)
        self.assertEqual(counts["slots_archived"], 0)
        self.assertEqual(counts["workouts_deleted"], 0)
        # Every new workout has a slot FK and PLANNED status.
        ws = list(Workout.objects.filter(plan=self.plan))
        self.assertEqual(len(ws), 4)
        for w in ws:
            self.assertIsNotNone(w.slot_id)
            self.assertEqual(w.status, "planned")
            self.assertEqual(w.source, "assistant")

    def test_apply_preserves_workout_uuid_when_slot_is_kept(self):
        rec1 = self._reconcile(self.plan, self.plan.schedule_json, self.plan.weeks, today=self.today)
        self._apply(rec1, plan=self.plan, tenant=self.tenant)
        original_uuids = set(Workout.objects.filter(plan=self.plan).values_list("id", flat=True))
        # A second reconcile + apply with the SAME schedule must NOT change uuids.
        rec2 = self._reconcile(self.plan, self.plan.schedule_json, self.plan.weeks, today=self.today)
        self._apply(rec2, plan=self.plan, tenant=self.tenant)
        new_uuids = set(Workout.objects.filter(plan=self.plan).values_list("id", flat=True))
        self.assertEqual(
            original_uuids, new_uuids, "Workout uuids must survive a noop reconcile — that's the whole point."
        )

    def test_apply_archives_slot_and_deletes_future_planned_workouts(self):
        rec = self._reconcile(self.plan, self.plan.schedule_json, weeks=4, today=self.today)
        self._apply(rec, plan=self.plan, tenant=self.tenant)
        # Shrink and apply.
        rec2 = self._reconcile(self.plan, self.plan.schedule_json, weeks=2, today=self.today)
        counts = self._apply(rec2, plan=self.plan, tenant=self.tenant)
        self.assertEqual(counts["slots_archived"], 4)
        self.assertEqual(counts["workouts_deleted"], 4)
        # Archived slots persist with archived_at set.
        archived = PlanSlot.objects.filter(plan=self.plan, archived_at__isnull=False)
        self.assertEqual(archived.count(), 4)
        # Remaining workouts only on weeks 0 and 1.
        weeks_left = set(Workout.objects.filter(plan=self.plan).values_list("slot__week_index", flat=True))
        self.assertEqual(weeks_left, {0, 1})

    def test_apply_preserves_done_workout_on_archived_slot(self):
        rec = self._reconcile(self.plan, self.plan.schedule_json, weeks=4, today=self.today)
        self._apply(rec, plan=self.plan, tenant=self.tenant)
        # Mark a week-3 workout as done.
        w_done = Workout.objects.get(plan=self.plan, slot__week_index=3, slot__weekday=0)
        w_done.status = "done"
        w_done.save()
        # Shrink to weeks=2.
        rec2 = self._reconcile(self.plan, self.plan.schedule_json, weeks=2, today=self.today)
        counts = self._apply(rec2, plan=self.plan, tenant=self.tenant)
        # Done workout survives; only PLANNED gets deleted.
        self.assertEqual(counts["workouts_deleted"], 3)  # 4 archived slots, 1 is done -> 3 deleted
        w_done.refresh_from_db()
        self.assertEqual(w_done.status, "done")
        # Its slot is now archived.
        self.assertIsNotNone(w_done.slot.archived_at)

    def test_edit_lock_skips_workout_deletion(self):
        from django.utils import timezone

        rec = self._reconcile(self.plan, self.plan.schedule_json, weeks=4, today=self.today)
        self._apply(rec, plan=self.plan, tenant=self.tenant)
        # Mid-edit lock on a soon-to-be-archived workout.
        w_locked = Workout.objects.get(plan=self.plan, slot__week_index=3, slot__weekday=0)
        w_locked.edit_lock_until = timezone.now() + timedelta(seconds=60)
        w_locked.edit_lock_owner = "user"
        w_locked.save(update_fields=["edit_lock_until", "edit_lock_owner"])
        # Shrink and apply with a lock check that honors the lock.
        rec2 = self._reconcile(self.plan, self.plan.schedule_json, weeks=2, today=self.today)

        def lock_check(w):
            return bool(w.edit_lock_until and w.edit_lock_until > timezone.now())

        counts = self._apply(rec2, plan=self.plan, tenant=self.tenant, edit_lock_check=lock_check)
        self.assertEqual(counts["workouts_locked_skip"], 1)
        self.assertEqual(counts["workouts_deleted"], 3)
        # Locked workout still exists; its slot IS archived (orphan-for-audit).
        w_locked.refresh_from_db()
        self.assertEqual(Workout.objects.filter(id=w_locked.id).count(), 1)
        self.assertIsNotNone(w_locked.slot.archived_at)

    def test_apply_is_atomic_on_failure(self):
        # If the create_workout step blows up, no slots should remain.
        # We simulate this by setting plan.start_date to None partway through.
        rec = self._reconcile(self.plan, self.plan.schedule_json, self.plan.weeks, today=self.today)
        # Patch the Workout.create to raise on the third call.
        from unittest.mock import patch as mock_patch

        from apps.fuel.models import Workout as WModel

        call_count = {"n": 0}
        original_create = WModel.objects.create

        def bombing_create(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 3:
                raise RuntimeError("boom")
            return original_create(*a, **kw)

        with (
            mock_patch.object(WModel.objects, "create", side_effect=bombing_create),
            self.assertRaises(RuntimeError),
        ):
            self._apply(rec, plan=self.plan, tenant=self.tenant)
        # Atomic transaction rolled back — no slots, no workouts.
        self.assertEqual(PlanSlot.objects.filter(plan=self.plan).count(), 0)
        self.assertEqual(Workout.objects.filter(plan=self.plan).count(), 0)


class WorkoutEditLockEndpointTests(TestCase):
    """Phase 4 — consumer-facing acquire/release."""

    def setUp(self):
        from django.utils import timezone

        self.tenant = create_tenant(display_name="Lock Test", telegram_chat_id=800200)
        self.user = self.tenant.user
        self.workout = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 6, 5),
            category="strength",
            activity="Push",
        )
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
        self._tz_now = timezone.now

    def _url(self, wid=None):
        return f"/api/v1/fuel/workouts/{wid or self.workout.id}/edit-lock/"

    def test_post_acquires_lock_with_ttl(self):
        before = self._tz_now()
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 200)
        self.workout.refresh_from_db()
        self.assertIsNotNone(self.workout.edit_lock_until)
        self.assertEqual(self.workout.edit_lock_owner, "user")
        delta = (self.workout.edit_lock_until - before).total_seconds()
        # TTL is 60 by default; allow a 5s margin for test scheduling.
        self.assertGreater(delta, 55)
        self.assertLess(delta, 70)
        self.assertEqual(resp.data["workout_id"], str(self.workout.id))
        self.assertEqual(resp.data["ttl_seconds"], 60)

    def test_post_heartbeat_extends_lock(self):
        r1 = self.client.post(self._url())
        first_until = r1.data["edit_lock_until"]
        # Re-acquire; the new edit_lock_until should be >= the first one.
        r2 = self.client.post(self._url())
        self.assertGreaterEqual(r2.data["edit_lock_until"], first_until)

    def test_delete_releases_lock(self):
        self.client.post(self._url())
        resp = self.client.delete(self._url())
        self.assertEqual(resp.status_code, 204)
        self.workout.refresh_from_db()
        self.assertIsNone(self.workout.edit_lock_until)
        self.assertEqual(self.workout.edit_lock_owner, "")

    def test_delete_is_idempotent_when_no_lock(self):
        resp = self.client.delete(self._url())
        self.assertEqual(resp.status_code, 204)

    def test_cannot_lock_other_tenants_workout(self):
        other_tenant = create_tenant(display_name="Other", telegram_chat_id=800201)
        other_workout = Workout.objects.create(
            tenant=other_tenant,
            date=date(2026, 6, 5),
            category="strength",
            activity="Push",
        )
        resp = self.client.post(self._url(wid=other_workout.id))
        self.assertEqual(resp.status_code, 404)


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeEditLockGateTests(TestCase):
    """Phase 4 — runtime PATCH / DELETE / skip / complete refuse when locked."""

    def setUp(self):
        from django.utils import timezone

        self.tenant = create_tenant(display_name="Gate Test", telegram_chat_id=800210)
        self.workout = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 6, 5),
            category="strength",
            activity="Push",
            status="planned",
        )
        self.workout.edit_lock_until = timezone.now() + timedelta(seconds=60)
        self.workout.edit_lock_owner = "user"
        self.workout.save(update_fields=["edit_lock_until", "edit_lock_owner"])
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _runtime_url(self, suffix=""):
        return f"/api/v1/fuel/runtime/{self.tenant.id}/workouts/{self.workout.id}/{suffix}"

    def test_patch_returns_409_with_retry_after(self):
        resp = self.client.patch(
            self._runtime_url(),
            data={"activity": "Renamed"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"], "edit_locked")
        self.assertIn("retry_after_s", resp.data)
        self.assertIn("Retry-After", resp.headers)
        self.workout.refresh_from_db()
        self.assertEqual(self.workout.activity, "Push")

    def test_delete_returns_409_when_locked(self):
        resp = self.client.delete(self._runtime_url(), **self.headers)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(Workout.objects.filter(id=self.workout.id).count(), 1)

    def test_skip_returns_409_when_locked(self):
        resp = self.client.post(
            self._runtime_url("skip/"),
            data={"reason": "from assistant"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 409)
        self.workout.refresh_from_db()
        self.assertEqual(self.workout.status, "planned")

    def test_complete_returns_409_when_locked(self):
        resp = self.client.post(
            self._runtime_url("complete/"),
            data={"notes": "from assistant"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 409)
        self.workout.refresh_from_db()
        self.assertEqual(self.workout.status, "planned")

    def test_expired_lock_does_not_gate(self):
        from django.utils import timezone

        self.workout.edit_lock_until = timezone.now() - timedelta(seconds=10)
        self.workout.save(update_fields=["edit_lock_until"])
        resp = self.client.patch(
            self._runtime_url(),
            data={"activity": "Renamed"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.workout.refresh_from_db()
        self.assertEqual(self.workout.activity, "Renamed")


class BodyWeightLogModelTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Weight Test", telegram_chat_id=800002)

    def test_create_entry(self):
        entry = BodyWeightLog.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            weight_kg=Decimal("82.50"),
        )
        self.assertEqual(entry.weight_kg, Decimal("82.50"))

    def test_unique_together_tenant_date(self):
        BodyWeightLog.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            weight_kg=Decimal("82.50"),
        )
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            BodyWeightLog.objects.create(
                tenant=self.tenant,
                date=date(2026, 4, 21),
                weight_kg=Decimal("83.00"),
            )


# ═════════════════════════════════════════════════════════════════════
# 3. Consumer View Tests
# ═════════════════════════════════════════════════════════════════════


class ConsumerFuelViewTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Consumer Test", telegram_chat_id=800003)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_settings_toggle(self):
        self.assertFalse(self.tenant.fuel_enabled)
        resp = self.client.patch("/api/v1/fuel/settings/", {"fuel_enabled": True}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["fuel_enabled"])
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.fuel_enabled)

    def test_create_workout(self):
        resp = self.client.post(
            "/api/v1/fuel/workouts/",
            {
                "date": "2026-04-21",
                "category": "strength",
                "activity": "Push Day",
                "duration_minutes": 60,
                "rpe": 7,
                "detail_json": {"exercises": []},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["activity"], "Push Day")

    def test_list_workouts(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 20),
            category="cardio",
            activity="Run",
        )
        resp = self.client.get("/api/v1/fuel/workouts/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)

    def test_list_workouts_filter_category(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 20),
            category="cardio",
            activity="Run",
        )
        resp = self.client.get("/api/v1/fuel/workouts/?category=strength")
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["category"], "strength")

    def test_workout_count(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 20),
            category="cardio",
            activity="Run",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 19),
            category="strength",
            activity="Pull",
            status="planned",
        )
        resp = self.client.get("/api/v1/fuel/workouts/count/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["count"], 3)

    def test_workout_count_with_status_filter(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 19),
            category="strength",
            activity="Pull",
            status="planned",
        )
        resp = self.client.get("/api/v1/fuel/workouts/count/?status=done")
        self.assertEqual(resp.data["count"], 1)

    def test_workout_count_tenant_isolation(self):
        other = create_tenant(display_name="Other", telegram_chat_id=800099)
        Workout.objects.create(
            tenant=other,
            date=date(2026, 4, 21),
            category="strength",
            activity="Other Push",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="My Push",
        )
        resp = self.client.get("/api/v1/fuel/workouts/count/")
        self.assertEqual(resp.data["count"], 1)

    def test_workout_detail_get(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        resp = self.client.get(f"/api/v1/fuel/workouts/{w.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["activity"], "Push")

    def test_workout_detail_patch(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        resp = self.client.patch(f"/api/v1/fuel/workouts/{w.id}/", {"rpe": 8}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["rpe"], 8)

    def test_workout_detail_delete(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        resp = self.client.delete(f"/api/v1/fuel/workouts/{w.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 0)

    def test_calendar_view(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="mobility",
            activity="Flow",
        )
        resp = self.client.get("/api/v1/fuel/calendar/?year=2026&month=4")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)  # one date entry
        self.assertEqual(len(resp.data[0]["workouts"]), 2)

    def test_tenant_isolation(self):
        other = create_tenant(display_name="Other", telegram_chat_id=800099)
        Workout.objects.create(
            tenant=other,
            date=date(2026, 4, 21),
            category="strength",
            activity="Other Push",
        )
        resp = self.client.get("/api/v1/fuel/workouts/")
        self.assertEqual(len(resp.data), 0)

    def test_body_weight_create(self):
        resp = self.client.post(
            "/api/v1/fuel/body-weight/",
            {"date": "2026-04-21", "weight_kg": "82.5"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["weight_kg"], "82.50")

    def test_body_weight_upsert(self):
        self.client.post(
            "/api/v1/fuel/body-weight/",
            {"date": "2026-04-21", "weight_kg": "82.5"},
            format="json",
        )
        resp = self.client.post(
            "/api/v1/fuel/body-weight/",
            {"date": "2026-04-21", "weight_kg": "83.0"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(BodyWeightLog.objects.filter(tenant=self.tenant).count(), 1)

    def test_body_weight_list(self):
        BodyWeightLog.objects.create(tenant=self.tenant, date=date(2026, 4, 21), weight_kg=Decimal("82.50"))
        BodyWeightLog.objects.create(tenant=self.tenant, date=date(2026, 4, 20), weight_kg=Decimal("82.30"))
        resp = self.client.get("/api/v1/fuel/body-weight/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)

    def test_body_weight_patch_changes_date_and_weight(self):
        entry = BodyWeightLog.objects.create(tenant=self.tenant, date=date(2026, 5, 15), weight_kg=Decimal("69.20"))
        resp = self.client.patch(
            f"/api/v1/fuel/body-weight/{entry.id}/",
            {"date": "2026-05-16", "weight_kg": "69.30"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(str(entry.date), "2026-05-16")
        self.assertEqual(str(entry.weight_kg), "69.30")

    def test_body_weight_patch_date_collision_returns_409(self):
        """Shifting an entry onto an occupied date must surface as a clean 409, not a 500."""
        target = BodyWeightLog.objects.create(tenant=self.tenant, date=date(2026, 5, 16), weight_kg=Decimal("69.40"))
        source = BodyWeightLog.objects.create(tenant=self.tenant, date=date(2026, 5, 15), weight_kg=Decimal("69.20"))
        resp = self.client.patch(
            f"/api/v1/fuel/body-weight/{source.id}/",
            {"date": "2026-05-16"},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"], "date_conflict")
        # Neither entry was mutated.
        source.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(str(source.date), "2026-05-15")
        self.assertEqual(str(target.weight_kg), "69.40")

    def test_body_weight_patch_weight_only(self):
        """Patching weight without changing date should not trip the collision check."""
        entry = BodyWeightLog.objects.create(tenant=self.tenant, date=date(2026, 5, 15), weight_kg=Decimal("69.20"))
        resp = self.client.patch(
            f"/api/v1/fuel/body-weight/{entry.id}/",
            {"weight_kg": "69.50"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(str(entry.weight_kg), "69.50")

    def test_create_rest_day(self):
        resp = self.client.post(
            "/api/v1/fuel/workouts/",
            {"date": "2026-04-21", "status": "rest", "category": "other", "activity": "Rest day"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["status"], "rest")

    def test_rest_day_in_calendar(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            status="rest",
            category="other",
            activity="Rest day",
        )
        resp = self.client.get("/api/v1/fuel/calendar/?year=2026&month=4")
        self.assertEqual(resp.status_code, 200)
        day = next(d for d in resp.data if d["date"] == "2026-04-21")
        self.assertEqual(len(day["workouts"]), 1)
        self.assertEqual(day["workouts"][0]["status"], "rest")

    def test_rest_day_not_in_done_count(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            status="rest",
            category="other",
            activity="Rest day",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 20),
            category="strength",
            activity="Push",
        )
        resp = self.client.get("/api/v1/fuel/workouts/count/?status=done")
        self.assertEqual(resp.data["count"], 1)

    def test_progress_strength(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 19),
            category="strength",
            activity="Push",
            status="done",
            detail_json={"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 70}]}]},
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
            status="done",
            detail_json={"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 75}]}]},
        )
        resp = self.client.get("/api/v1/fuel/progress/?category=strength")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bench", resp.data["progress"])
        self.assertEqual(len(resp.data["progress"]["Bench"]), 2)


# ═════════════════════════════════════════════════════════════════════
# 3b. Session lifecycle (skip / complete / swap / scheduled_at)
# ═════════════════════════════════════════════════════════════════════


class WorkoutSessionLifecycleTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Session Test", telegram_chat_id=800099)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def _make(self, **kw):
        defaults = dict(
            tenant=self.tenant,
            date=date(2026, 4, 28),
            category="strength",
            activity="Push",
            status="planned",
        )
        defaults.update(kw)
        return Workout.objects.create(**defaults)

    def test_create_with_scheduled_at_derives_date(self):
        from datetime import datetime

        resp = self.client.post(
            "/api/v1/fuel/workouts/",
            {
                "scheduled_at": datetime(2026, 5, 1, 7, 30, tzinfo=UTC).isoformat(),
                "category": "strength",
                "activity": "Push",
                "status": "planned",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["date"], "2026-05-01")
        self.assertIsNotNone(resp.data["scheduled_at"])

    def test_skip_sets_status_and_reason(self):
        w = self._make()
        resp = self.client.post(f"/api/v1/fuel/workouts/{w.id}/skip/", {"reason": "traveling"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "skipped")
        self.assertEqual(resp.data["skip_reason"], "traveling")

    def test_skip_truncates_long_reason(self):
        w = self._make()
        long_reason = "x" * 200
        resp = self.client.post(f"/api/v1/fuel/workouts/{w.id}/skip/", {"reason": long_reason}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["skip_reason"]), 128)

    def test_complete_marks_done_with_optional_fields(self):
        w = self._make()
        resp = self.client.post(
            f"/api/v1/fuel/workouts/{w.id}/complete/",
            {"notes": "felt strong", "rpe": 8, "duration_minutes": 55},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "done")
        self.assertEqual(resp.data["notes"], "felt strong")
        self.assertEqual(resp.data["rpe"], 8)
        self.assertEqual(resp.data["duration_minutes"], 55)

    def test_complete_ignores_invalid_rpe(self):
        w = self._make(rpe=5)
        resp = self.client.post(f"/api/v1/fuel/workouts/{w.id}/complete/", {"rpe": 99}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["rpe"], 5)

    def test_swap_exchanges_scheduled_at_and_date(self):
        from datetime import datetime

        a = self._make(date=date(2026, 4, 28), scheduled_at=datetime(2026, 4, 28, 7, 0, tzinfo=UTC))
        b = self._make(date=date(2026, 4, 30), scheduled_at=datetime(2026, 4, 30, 18, 0, tzinfo=UTC))
        resp = self.client.post("/api/v1/fuel/workouts/swap/", {"a": str(a.id), "b": str(b.id)}, format="json")
        self.assertEqual(resp.status_code, 200)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.date, date(2026, 4, 30))
        self.assertEqual(b.date, date(2026, 4, 28))
        self.assertEqual(a.scheduled_at.hour, 18)
        self.assertEqual(b.scheduled_at.hour, 7)

    def test_swap_rejects_same_id(self):
        a = self._make()
        resp = self.client.post("/api/v1/fuel/workouts/swap/", {"a": str(a.id), "b": str(a.id)}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_swap_404_on_missing(self):
        a = self._make()
        import uuid

        resp = self.client.post("/api/v1/fuel/workouts/swap/", {"a": str(a.id), "b": str(uuid.uuid4())}, format="json")
        self.assertEqual(resp.status_code, 404)

    def test_list_window_query_param(self):
        from datetime import timedelta

        today = date.today()
        self._make(date=today)  # in window
        self._make(date=today + timedelta(days=3))  # in window
        self._make(date=today + timedelta(days=14))  # out of window
        self._make(date=today - timedelta(days=2))  # out of window (past)
        resp = self.client.get("/api/v1/fuel/workouts/?window=7d")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)


# ═════════════════════════════════════════════════════════════════════
# 4. Runtime View Tests
# ═════════════════════════════════════════════════════════════════════


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeFuelViewTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Runtime Test", telegram_chat_id=800010)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_log_workout(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {
                "date": "2026-04-21",
                "category": "strength",
                "activity": "Push Day",
                "duration_minutes": 60,
                "rpe": 7,
                "detail_json": {"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 75}]}]},
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["activity"], "Push Day")
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 1)

    def test_log_workout_invalid_category_defaults(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {"date": "2026-04-21", "category": "nonsense", "activity": "Whatever"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["category"], "other")

    def test_auth_required(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {"date": "2026-04-21", "category": "strength", "activity": "Test"},
            format="json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_wrong_key(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {"date": "2026-04-21", "category": "strength", "activity": "Test"},
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="wrong-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 401)

    def test_summary(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/summary/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["recent_workouts"]), 1)

    def test_log_body_weight(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/body-weight/",
            {"date": "2026-04-21", "weight_kg": "82.5"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertIn("82.5", resp.data["weight_kg"])

    def test_log_body_weight_default_date_uses_tenant_timezone(self):
        """Bug #3 regression: an early-morning Eastern entry lands today, not yesterday.

        Without a tz-aware default, a 6 AM EDT log on May 16 = 10 AM UTC
        May 16 would store May 16 — fine. But a 9 PM EDT log on May 16 =
        1 AM UTC May 17 would store May 17 (UTC's "today") instead of
        May 16 (the user's). We freeze time at the boundary case to
        prove the server consults the tenant's tz.
        """
        from datetime import UTC, datetime
        from unittest.mock import patch

        # Tenant user is in America/New_York.
        self.tenant.user.timezone = "America/New_York"
        self.tenant.user.save(update_fields=["timezone"])

        # 2026-05-17 03:00 UTC = 2026-05-16 23:00 EDT. Eastern is still on May 16.
        frozen = datetime(2026, 5, 17, 3, 0, 0, tzinfo=UTC)
        with patch("apps.common.llm_contracts.dj_tz.now", return_value=frozen):
            resp = self.client.post(
                f"/api/v1/fuel/runtime/{self.tenant.id}/body-weight/",
                {"weight_kg": "82.5"},  # No date — should default to today-in-tenant-tz.
                format="json",
                **self.headers,
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["date"], "2026-05-16")

    def test_log_body_weight_accepts_relative_phrase(self):
        """The new contract: 'today' / 'yesterday' / weekday phrases resolve server-side."""
        from datetime import UTC, datetime
        from unittest.mock import patch

        self.tenant.user.timezone = "America/New_York"
        self.tenant.user.save(update_fields=["timezone"])

        frozen = datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC)  # 10am EDT May 17.
        with patch("apps.common.llm_contracts.dj_tz.now", return_value=frozen):
            resp = self.client.post(
                f"/api/v1/fuel/runtime/{self.tenant.id}/body-weight/",
                {"date": "yesterday", "weight_kg": "82.5"},
                format="json",
                **self.headers,
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["date"], "2026-05-16")

    def test_tenant_isolation_runtime(self):
        other = create_tenant(display_name="Other", telegram_chat_id=800099)
        Workout.objects.create(
            tenant=other,
            date=date(2026, 4, 21),
            category="strength",
            activity="Other",
        )
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/summary/",
            **self.headers,
        )
        self.assertEqual(len(resp.data["recent_workouts"]), 0)


# ═════════════════════════════════════════════════════════════════════
# 4b. Runtime Session Lifecycle (skip / complete / swap mirrors)
# ═════════════════════════════════════════════════════════════════════


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeSessionLifecycleTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Runtime Session Test", telegram_chat_id=800077)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _make(self, **kw):
        defaults = dict(
            tenant=self.tenant,
            date=date(2026, 4, 28),
            category="strength",
            activity="Push",
            status="planned",
        )
        defaults.update(kw)
        return Workout.objects.create(**defaults)

    def test_runtime_skip(self):
        w = self._make()
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/workouts/{w.id}/skip/",
            {"reason": "kid sick"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "skipped")
        self.assertEqual(resp.data["skip_reason"], "kid sick")

    def test_runtime_skip_unauthorized(self):
        w = self._make()
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/workouts/{w.id}/skip/",
            {"reason": "x"},
            format="json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_runtime_complete(self):
        w = self._make()
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/workouts/{w.id}/complete/",
            {"rpe": 7, "duration_minutes": 50, "notes": "good push session"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "done")
        self.assertEqual(resp.data["rpe"], 7)

    def test_runtime_swap(self):
        from datetime import datetime

        a = self._make(date=date(2026, 4, 28), scheduled_at=datetime(2026, 4, 28, 7, 0, tzinfo=UTC))
        b = self._make(date=date(2026, 4, 30), scheduled_at=datetime(2026, 4, 30, 18, 0, tzinfo=UTC))
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/workouts/swap/",
            {"a": str(a.id), "b": str(b.id)},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.date, date(2026, 4, 30))
        self.assertEqual(b.date, date(2026, 4, 28))


# ═════════════════════════════════════════════════════════════════════
# 4c. Runtime Audit (cross-reference today_plan + crons + workouts)
# ═════════════════════════════════════════════════════════════════════


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeFuelAuditTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Audit Test", telegram_chat_id=800301)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _make(self, **kw):
        defaults = dict(
            tenant=self.tenant,
            date=date.today(),
            category="strength",
            activity="Push",
            status="planned",
        )
        defaults.update(kw)
        return Workout.objects.create(**defaults)

    def _set_daily_fuel_section(self, body: str, day=None) -> None:
        from apps.journal.models import Document

        d = day or date.today()
        Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug=str(d),
            title=f"Daily Note {d}",
            markdown=f"# Daily Note\n\n## Fuel\n{body}\n\n## Next\n",
        )

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_audit_no_today_plan_no_crons(self, mock_invoke):
        mock_invoke.return_value = {"details": {"jobs": []}}
        resp = self.client.get(f"/api/v1/fuel/runtime/{self.tenant.id}/audit/", **self.headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.data
        self.assertFalse(body["today_plan"]["exists"])
        self.assertEqual(body["next_14d_workouts"], [])
        self.assertEqual(body["fuel_crons"], [])
        self.assertEqual(body["conflicts"]["duplicate_fires"], [])
        self.assertIn("Safe to propose", body["guidance"])

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_audit_returns_today_plan_when_section_present(self, mock_invoke):
        mock_invoke.return_value = {"details": {"jobs": []}}
        self._set_daily_fuel_section("**Today:** Push Day — Chest & Shoulders\n**Plan:** 4-Week Builder")
        resp = self.client.get(f"/api/v1/fuel/runtime/{self.tenant.id}/audit/", **self.headers)
        self.assertTrue(resp.data["today_plan"]["exists"])
        self.assertIn("Push Day", resp.data["today_plan"]["raw_section"])
        # Guidance should point the LLM at workout IDs for updates/deletes, not at summary.
        self.assertIn("next_14d_workouts[i].id", resp.data["guidance"])
        self.assertIn("nbhd_fuel_update_workout", resp.data["guidance"])

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_audit_no_plan_guidance_mentions_workout_ids(self, mock_invoke):
        """Even without today_plan, guidance must surface that IDs are inline."""
        mock_invoke.return_value = {"details": {"jobs": []}}
        resp = self.client.get(f"/api/v1/fuel/runtime/{self.tenant.id}/audit/", **self.headers)
        self.assertFalse(resp.data["today_plan"]["exists"])
        self.assertIn("next_14d_workouts[i].id", resp.data["guidance"])

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_audit_lists_next_14d_workouts(self, mock_invoke):
        from datetime import timedelta

        mock_invoke.return_value = {"details": {"jobs": []}}
        today = date.today()
        self._make(date=today + timedelta(days=2), activity="Pull Day")
        self._make(date=today + timedelta(days=4), activity="Leg Day", category="strength")
        # Out of horizon
        self._make(date=today + timedelta(days=20), activity="Far Future")
        resp = self.client.get(f"/api/v1/fuel/runtime/{self.tenant.id}/audit/", **self.headers)
        activities = [w["activity"] for w in resp.data["next_14d_workouts"]]
        self.assertIn("Pull Day", activities)
        self.assertIn("Leg Day", activities)
        self.assertNotIn("Far Future", activities)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_audit_flags_duplicate_fires(self, mock_invoke):
        mock_invoke.return_value = {
            "details": {
                "jobs": [
                    {
                        "name": "_fuel:abcd1234",
                        "id": "j1",
                        "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
                    },
                    {
                        "name": "user-leg-day",
                        "id": "j2",
                        "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
                    },
                ]
            }
        }
        resp = self.client.get(f"/api/v1/fuel/runtime/{self.tenant.id}/audit/", **self.headers)
        dupes = resp.data["conflicts"]["duplicate_fires"]
        self.assertEqual(len(dupes), 1)
        self.assertCountEqual(dupes[0]["crons"], ["_fuel:abcd1234", "user-leg-day"])
        self.assertIn("STOP", resp.data["guidance"])

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_audit_flags_orphan_crons(self, mock_invoke):
        mock_invoke.return_value = {
            "details": {
                "jobs": [
                    {
                        "name": "_fuel:deadbeef",
                        "id": "j-orphan",
                        "schedule": {"kind": "cron", "expr": "0 18 1 5 *", "tz": "UTC"},
                    }
                ]
            }
        }
        resp = self.client.get(f"/api/v1/fuel/runtime/{self.tenant.id}/audit/", **self.headers)
        orphans = resp.data["conflicts"]["orphan_crons"]
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["name"], "_fuel:deadbeef")

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_audit_recovers_from_cron_list_failure(self, mock_invoke):
        from apps.cron.gateway_client import GatewayError

        mock_invoke.side_effect = GatewayError("container 503")
        resp = self.client.get(f"/api/v1/fuel/runtime/{self.tenant.id}/audit/", **self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["fuel_crons"], [])
        self.assertIsNotNone(resp.data["cron_list_error"])

    def test_audit_unauthorized(self):
        resp = self.client.get(f"/api/v1/fuel/runtime/{self.tenant.id}/audit/")
        self.assertEqual(resp.status_code, 401)


# ═════════════════════════════════════════════════════════════════════
# 5. FuelProfile Model Tests
# ═════════════════════════════════════════════════════════════════════


class FuelProfileModelTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Profile Test", telegram_chat_id=800020)

    def test_create_profile(self):

        profile = FuelProfile.objects.create(tenant=self.tenant)
        self.assertEqual(profile.onboarding_status, "pending")
        self.assertEqual(profile.fitness_level, "")
        self.assertEqual(profile.goals, [])
        self.assertEqual(profile.limitations, [])
        self.assertEqual(profile.equipment, [])
        self.assertIsNone(profile.days_per_week)

    def test_one_to_one_constraint(self):
        from django.db import IntegrityError

        FuelProfile.objects.create(tenant=self.tenant)
        with self.assertRaises(IntegrityError):
            FuelProfile.objects.create(tenant=self.tenant)

    def test_status_transitions(self):

        profile = FuelProfile.objects.create(tenant=self.tenant)
        profile.onboarding_status = "in_progress"
        profile.save(update_fields=["onboarding_status"])
        profile.refresh_from_db()
        self.assertEqual(profile.onboarding_status, "in_progress")

        profile.onboarding_status = "completed"
        profile.save(update_fields=["onboarding_status"])
        profile.refresh_from_db()
        self.assertEqual(profile.onboarding_status, "completed")


# ═════════════════════════════════════════════════════════════════════
# 6. Fuel Settings Toggle with Profile
# ═════════════════════════════════════════════════════════════════════


class FuelSettingsToggleWithProfileTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Toggle Test", telegram_chat_id=800021)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_enable_creates_profile(self):

        resp = self.client.patch("/api/v1/fuel/settings/", {"fuel_enabled": True}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["fuel_enabled"])
        self.assertEqual(resp.data["fuel_profile_status"], "pending")
        self.assertTrue(FuelProfile.objects.filter(tenant=self.tenant).exists())

    def test_enable_idempotent(self):

        # First enable
        self.client.patch("/api/v1/fuel/settings/", {"fuel_enabled": True}, format="json")
        profile = FuelProfile.objects.get(tenant=self.tenant)
        profile.onboarding_status = "completed"
        profile.fitness_level = "intermediate"
        profile.save(update_fields=["onboarding_status", "fitness_level"])

        # Second enable — should not reset
        resp = self.client.patch("/api/v1/fuel/settings/", {"fuel_enabled": True}, format="json")
        self.assertEqual(resp.data["fuel_profile_status"], "completed")
        profile.refresh_from_db()
        self.assertEqual(profile.fitness_level, "intermediate")

    def test_disable_preserves_profile(self):

        self.client.patch("/api/v1/fuel/settings/", {"fuel_enabled": True}, format="json")
        profile = FuelProfile.objects.get(tenant=self.tenant)
        profile.onboarding_status = "completed"
        profile.save(update_fields=["onboarding_status"])

        # Disable
        resp = self.client.patch("/api/v1/fuel/settings/", {"fuel_enabled": False}, format="json")
        self.assertFalse(resp.data["fuel_enabled"])
        # Profile still exists
        self.assertTrue(FuelProfile.objects.filter(tenant=self.tenant).exists())
        profile.refresh_from_db()
        self.assertEqual(profile.onboarding_status, "completed")

    def test_toggle_cycle_preserves_completed_profile(self):

        # Enable → complete profile → disable → re-enable
        self.client.patch("/api/v1/fuel/settings/", {"fuel_enabled": True}, format="json")
        profile = FuelProfile.objects.get(tenant=self.tenant)
        profile.onboarding_status = "completed"
        profile.goals = ["strength", "endurance"]
        profile.save(update_fields=["onboarding_status", "goals"])

        self.client.patch("/api/v1/fuel/settings/", {"fuel_enabled": False}, format="json")
        resp = self.client.patch("/api/v1/fuel/settings/", {"fuel_enabled": True}, format="json")
        self.assertEqual(resp.data["fuel_profile_status"], "completed")
        profile.refresh_from_db()
        self.assertEqual(profile.goals, ["strength", "endurance"])


# ═════════════════════════════════════════════════════════════════════
# 7. Consumer FuelProfile View Tests
# ═════════════════════════════════════════════════════════════════════


class ConsumerFuelProfileViewTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Profile View Test", telegram_chat_id=800022)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_get_profile_404_when_none(self):
        resp = self.client.get("/api/v1/fuel/profile/")
        self.assertEqual(resp.status_code, 404)

    def test_get_profile(self):

        FuelProfile.objects.create(
            tenant=self.tenant,
            onboarding_status="completed",
            fitness_level="intermediate",
            goals=["strength"],
            days_per_week=4,
        )
        resp = self.client.get("/api/v1/fuel/profile/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["fitness_level"], "intermediate")
        self.assertEqual(resp.data["goals"], ["strength"])

    def test_patch_profile(self):

        FuelProfile.objects.create(tenant=self.tenant)
        resp = self.client.patch(
            "/api/v1/fuel/profile/",
            {"fitness_level": "advanced", "days_per_week": 5},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["fitness_level"], "advanced")
        self.assertEqual(resp.data["days_per_week"], 5)


# ═════════════════════════════════════════════════════════════════════
# 8. Runtime FuelProfile View Tests
# ═════════════════════════════════════════════════════════════════════


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeFuelProfileViewTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="RT Profile Test", telegram_chat_id=800023)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_get_profile(self):

        FuelProfile.objects.create(
            tenant=self.tenant,
            onboarding_status="completed",
            fitness_level="beginner",
            goals=["weight_loss"],
        )
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/profile/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["fitness_level"], "beginner")
        self.assertEqual(resp.data["goals"], ["weight_loss"])

    def test_get_profile_404(self):
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/profile/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 404)

    def test_patch_progressive_update(self):

        FuelProfile.objects.create(tenant=self.tenant)

        # First update — fitness level
        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/profile/",
            {"onboarding_status": "in_progress", "fitness_level": "intermediate"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["onboarding_status"], "in_progress")
        self.assertEqual(resp.data["fitness_level"], "intermediate")

        # Second update — goals
        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/profile/",
            {"goals": ["strength", "endurance"]},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["goals"], ["strength", "endurance"])
        # fitness_level preserved
        self.assertEqual(resp.data["fitness_level"], "intermediate")

    def test_patch_creates_profile_if_missing(self):
        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/profile/",
            {"onboarding_status": "declined"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["onboarding_status"], "declined")

    def test_auth_required(self):
        resp = self.client.get(f"/api/v1/fuel/runtime/{self.tenant.id}/profile/")
        self.assertEqual(resp.status_code, 401)


# ═════════════════════════════════════════════════════════════════════
# 9. Runtime Summary Includes Profile
# ═════════════════════════════════════════════════════════════════════


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeFuelSummaryWithProfileTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Summary Profile", telegram_chat_id=800024)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_summary_includes_profile(self):

        FuelProfile.objects.create(
            tenant=self.tenant,
            onboarding_status="completed",
            fitness_level="advanced",
            goals=["muscle_gain"],
            days_per_week=6,
        )
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/summary/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("profile", resp.data)
        self.assertEqual(resp.data["profile"]["fitness_level"], "advanced")
        self.assertEqual(resp.data["profile"]["onboarding_status"], "completed")

    def test_summary_profile_null_when_none(self):
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/summary/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["profile"])


# ═════════════════════════════════════════════════════════════════════
# Templates, Duplicate, Weekly Volume, PRs, Goals, Resting HR
# ═════════════════════════════════════════════════════════════════════


class WorkoutTemplateTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Template Test", telegram_chat_id=800050)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_create_template(self):
        resp = self.client.post(
            "/api/v1/fuel/templates/",
            {
                "name": "Push Day A",
                "category": "strength",
                "activity": "Push — Chest & Shoulders",
                "detail_json": {"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 75}]}]},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["name"], "Push Day A")

    def test_list_templates(self):
        from .models import WorkoutTemplate

        WorkoutTemplate.objects.create(
            tenant=self.tenant,
            name="A",
            category="strength",
            activity="Push",
        )
        WorkoutTemplate.objects.create(
            tenant=self.tenant,
            name="B",
            category="cardio",
            activity="Run",
        )
        resp = self.client.get("/api/v1/fuel/templates/")
        self.assertEqual(len(resp.data), 2)

    def test_list_templates_filter_category(self):
        from .models import WorkoutTemplate

        WorkoutTemplate.objects.create(tenant=self.tenant, name="A", category="strength", activity="Push")
        WorkoutTemplate.objects.create(tenant=self.tenant, name="B", category="cardio", activity="Run")
        resp = self.client.get("/api/v1/fuel/templates/?category=strength")
        self.assertEqual(len(resp.data), 1)

    def test_delete_template(self):
        from .models import WorkoutTemplate

        tmpl = WorkoutTemplate.objects.create(tenant=self.tenant, name="A", category="strength", activity="Push")
        resp = self.client.delete(f"/api/v1/fuel/templates/{tmpl.id}/")
        self.assertEqual(resp.status_code, 204)


class WorkoutDuplicateTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Dup Test", telegram_chat_id=800051)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_duplicate_workout(self):
        source = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 20),
            category="strength",
            activity="Push Day",
            duration_minutes=60,
            detail_json={"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 75}]}]},
        )
        resp = self.client.post(f"/api/v1/fuel/workouts/{source.id}/duplicate/")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["activity"], "Push Day")
        self.assertEqual(resp.data["status"], "planned")
        self.assertNotEqual(resp.data["id"], str(source.id))
        self.assertEqual(resp.data["detail_json"]["exercises"][0]["name"], "Bench")


class WeeklyVolumeTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Volume Test", telegram_chat_id=800052)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_weekly_summary(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date.today(),
            category="strength",
            activity="Push",
            duration_minutes=60,
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date.today(),
            category="cardio",
            activity="Run",
            duration_minutes=30,
        )
        resp = self.client.get("/api/v1/fuel/weekly-summary/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["totals"]["sessions"], 2)
        self.assertEqual(resp.data["totals"]["minutes"], 90)

    def test_weekly_summary_empty(self):
        resp = self.client.get("/api/v1/fuel/weekly-summary/")
        self.assertEqual(resp.data["totals"]["sessions"], 0)


class PRDetectionTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="PR Test", telegram_chat_id=800053)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_pr_detected_on_create(self):
        from .models import PersonalRecord

        resp = self.client.post(
            "/api/v1/fuel/workouts/",
            {
                "date": "2026-04-21",
                "category": "strength",
                "activity": "Push",
                "detail_json": {"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 75}]}]},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(PersonalRecord.objects.filter(tenant=self.tenant).count(), 1)
        pr = PersonalRecord.objects.first()
        self.assertEqual(pr.exercise_name, "Bench")
        self.assertIsNone(pr.previous_value)

    def test_pr_updated_on_improvement(self):
        from .models import PersonalRecord

        # First workout
        self.client.post(
            "/api/v1/fuel/workouts/",
            {
                "date": "2026-04-20",
                "category": "strength",
                "activity": "Push",
                "detail_json": {"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 70}]}]},
            },
            format="json",
        )
        # Second workout with heavier weight
        self.client.post(
            "/api/v1/fuel/workouts/",
            {
                "date": "2026-04-21",
                "category": "strength",
                "activity": "Push",
                "detail_json": {"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 80}]}]},
            },
            format="json",
        )
        prs = PersonalRecord.objects.filter(tenant=self.tenant, exercise_name="Bench").order_by("created_at")
        self.assertEqual(prs.count(), 2)
        latest = prs.last()
        self.assertIsNotNone(latest.previous_value)

    def test_pr_feed(self):
        from .models import PersonalRecord

        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
            detail_json={"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 80}]}]},
        )
        PersonalRecord.objects.create(
            tenant=self.tenant,
            workout=w,
            exercise_name="Bench",
            category="strength",
            value=Decimal("99.0"),
            metric="est_1rm",
            date=date(2026, 4, 21),
        )
        resp = self.client.get("/api/v1/fuel/prs/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)


class FuelGoalTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Goal Test", telegram_chat_id=800054)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_create_goal(self):
        resp = self.client.post(
            "/api/v1/fuel/goals/",
            {"exercise_name": "Bench Press", "metric": "est_1rm", "target_value": "100.0", "target_date": "2026-06-01"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["exercise_name"], "Bench Press")

    def test_list_goals(self):
        from .models import FuelGoal

        FuelGoal.objects.create(tenant=self.tenant, exercise_name="Bench", target_value=Decimal("100"))
        FuelGoal.objects.create(tenant=self.tenant, exercise_name="Squat", target_value=Decimal("150"))
        resp = self.client.get("/api/v1/fuel/goals/")
        self.assertEqual(len(resp.data), 2)

    def test_delete_goal(self):
        from .models import FuelGoal

        goal = FuelGoal.objects.create(tenant=self.tenant, exercise_name="Bench", target_value=Decimal("100"))
        resp = self.client.delete(f"/api/v1/fuel/goals/{goal.id}/")
        self.assertEqual(resp.status_code, 204)


class RestingHeartRateTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="RHR Test", telegram_chat_id=800055)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_create_rhr(self):
        resp = self.client.post(
            "/api/v1/fuel/resting-hr/",
            {"date": "2026-04-21", "bpm": 62},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["bpm"], 62)

    def test_rhr_upsert(self):
        from .models import RestingHeartRateLog

        self.client.post("/api/v1/fuel/resting-hr/", {"date": "2026-04-21", "bpm": 62}, format="json")
        resp = self.client.post("/api/v1/fuel/resting-hr/", {"date": "2026-04-21", "bpm": 58}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(RestingHeartRateLog.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(resp.data["bpm"], 58)

    def test_rhr_list(self):
        from .models import RestingHeartRateLog

        RestingHeartRateLog.objects.create(tenant=self.tenant, date=date(2026, 4, 21), bpm=62)
        RestingHeartRateLog.objects.create(tenant=self.tenant, date=date(2026, 4, 20), bpm=60)
        resp = self.client.get("/api/v1/fuel/resting-hr/")
        self.assertEqual(len(resp.data), 2)

    def test_rhr_delete(self):
        from .models import RestingHeartRateLog

        entry = RestingHeartRateLog.objects.create(tenant=self.tenant, date=date(2026, 4, 21), bpm=62)
        resp = self.client.delete(f"/api/v1/fuel/resting-hr/{entry.id}/")
        self.assertEqual(resp.status_code, 204)


# ═════════════════════════════════════════════════════════════════════
# Workout Plan Tests
# ═════════════════════════════════════════════════════════════════════


class WorkoutPlanModelTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Plan Model Test", telegram_chat_id=800060)

    def test_create_plan(self):
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="4-Week Strength",
            start_date=date(2026, 4, 27),
            weeks=4,
            days_per_week=3,
            schedule_json={
                "0": {"activity": "Push", "category": "strength"},
                "2": {"activity": "Pull", "category": "strength"},
                "4": {"activity": "Legs", "category": "strength"},
            },
        )
        self.assertEqual(plan.status, "active")
        self.assertEqual(plan.weeks, 4)

    def test_workout_plan_fk(self):
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Test Plan",
            start_date=date(2026, 4, 27),
            weeks=2,
            days_per_week=2,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        w = Workout.objects.create(
            tenant=self.tenant,
            plan=plan,
            date=date(2026, 4, 27),
            status="planned",
            category="strength",
            activity="Push",
        )
        self.assertEqual(w.plan, plan)
        self.assertEqual(plan.workouts.count(), 1)

    def test_plan_delete_set_null(self):
        """Deleting a plan sets workout.plan to NULL instead of cascading."""
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Temp Plan",
            start_date=date(2026, 4, 27),
            weeks=1,
            days_per_week=1,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        w = Workout.objects.create(
            tenant=self.tenant,
            plan=plan,
            date=date(2026, 4, 27),
            status="done",
            category="strength",
            activity="Push",
        )
        plan.delete()
        w.refresh_from_db()
        self.assertIsNone(w.plan)

    def test_profile_preferred_fields(self):
        profile = FuelProfile.objects.create(
            tenant=self.tenant,
            preferred_days=[0, 2, 4],
            preferred_time="morning",
        )
        profile.refresh_from_db()
        self.assertEqual(profile.preferred_days, [0, 2, 4])
        self.assertEqual(profile.preferred_time, "morning")


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeWorkoutPlanTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="RT Plan Test", telegram_chat_id=800061)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_create_plan(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/",
            {
                "name": "4-Week Strength",
                "start_date": "2026-04-27",
                "weeks": 4,
                "days_per_week": 3,
                "schedule_json": {
                    "0": {"activity": "Push", "category": "strength", "duration_minutes": 60},
                    "2": {"activity": "Pull", "category": "strength", "duration_minutes": 60},
                    "4": {"activity": "Legs", "category": "strength", "duration_minutes": 55},
                },
                "notes": "Linear progression: add 2.5kg each week.",
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["name"], "4-Week Strength")
        self.assertEqual(resp.data["status"], "active")
        # 4 weeks x 3 days = 12 planned workouts
        self.assertEqual(resp.data["workouts_created"], 12)
        self.assertEqual(Workout.objects.filter(tenant=self.tenant, status="planned").count(), 12)

    def test_create_plan_date_math(self):
        """Workouts land on correct weekdays."""
        self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/",
            {
                "name": "Test Dates",
                "start_date": "2026-04-27",  # Monday
                "weeks": 1,
                "days_per_week": 2,
                "schedule_json": {
                    "0": {"activity": "Mon Workout", "category": "strength"},
                    "4": {"activity": "Fri Workout", "category": "cardio"},
                },
            },
            format="json",
            **self.headers,
        )
        workouts = Workout.objects.filter(tenant=self.tenant).order_by("date")
        self.assertEqual(workouts.count(), 2)
        # 2026-04-27 is a Monday (weekday=0)
        self.assertEqual(workouts[0].date, date(2026, 4, 27))
        self.assertEqual(workouts[0].activity, "Mon Workout")
        # 2026-05-01 is a Friday (weekday=4)
        self.assertEqual(workouts[1].date, date(2026, 5, 1))
        self.assertEqual(workouts[1].activity, "Fri Workout")

    def test_list_plans(self):
        WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Plan A",
            start_date=date(2026, 4, 27),
            weeks=4,
            days_per_week=3,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["plans"]), 1)

    def test_get_plan_detail(self):
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Detail Plan",
            start_date=date(2026, 4, 27),
            weeks=1,
            days_per_week=1,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        Workout.objects.create(
            tenant=self.tenant,
            plan=plan,
            date=date(2026, 4, 27),
            status="planned",
            category="strength",
            activity="Push",
        )
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{plan.id}/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["name"], "Detail Plan")
        self.assertEqual(len(resp.data["workouts"]), 1)

    def test_update_plan_status(self):
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Pause Me",
            start_date=date(2026, 4, 27),
            weeks=4,
            days_per_week=3,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{plan.id}/",
            {"status": "paused"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "paused")

    def test_update_plan_preserves_detail_json(self):
        """Regen preserves per-workout exercise prescriptions when the new
        schedule_json doesn't override them. Under the reconciler (phase 5)
        the existing workout row is adopted to a slot in place — its uuid
        stays the same, and all user fields the new template doesn't speak
        to are left untouched.
        """
        from datetime import timedelta

        plan_start = date.today() + timedelta(days=((7 - date.today().weekday()) % 7) or 7)
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Soccer Build",
            start_date=plan_start,
            weeks=2,
            days_per_week=2,
            schedule_json={
                "0": {"activity": "Upper Push", "category": "strength"},
                "2": {"activity": "Upper Pull", "category": "strength"},
            },
        )
        customised = Workout.objects.create(
            tenant=self.tenant,
            plan=plan,
            date=plan_start,
            status="planned",
            category="strength",
            activity="Upper Push",
            duration_minutes=55,
            detail_json={
                "exercises": [
                    {"name": "Bench Press", "sets": [{"type": "weighted_reps", "reps": 5, "weight": 80}]},
                ]
            },
            notes="Pre-meet taper week",
        )

        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{plan.id}/",
            {
                "schedule_json": {
                    "0": {"activity": "Upper Push", "category": "strength"},
                    "2": {"activity": "Upper Pull", "category": "strength"},
                },
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)

        regen = Workout.objects.get(tenant=self.tenant, plan=plan, date=plan_start, activity="Upper Push")
        # Reconciler keeps the uuid stable — the row is the same row.
        self.assertEqual(regen.id, customised.id)
        self.assertEqual(regen.detail_json, customised.detail_json)
        self.assertEqual(regen.duration_minutes, 55)
        self.assertEqual(regen.notes, "Pre-meet taper week")
        # And it now has a slot FK (adopted by the reconciler).
        self.assertIsNotNone(regen.slot_id)

    def test_update_plan_template_overrides_duration(self):
        """When the new schedule_json supplies duration_minutes, it wins
        over the preserved snapshot.
        """
        from datetime import timedelta

        plan_start = date.today() + timedelta(days=((7 - date.today().weekday()) % 7) or 7)
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Override",
            start_date=plan_start,
            weeks=1,
            days_per_week=1,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        Workout.objects.create(
            tenant=self.tenant,
            plan=plan,
            date=plan_start,
            status="planned",
            category="strength",
            activity="Push",
            duration_minutes=45,
        )

        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{plan.id}/",
            {
                "schedule_json": {
                    "0": {"activity": "Push", "category": "strength", "duration_minutes": 75},
                },
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        regen = Workout.objects.get(plan=plan, date=plan_start, activity="Push")
        self.assertEqual(regen.duration_minutes, 75)

    def test_update_plan_renamed_activity_propagates_to_slot(self):
        """If the schedule renames the activity for a weekday, the slot's
        existing workout is renamed in place (uuid stable). Detail fields
        the new template doesn't speak to are left alone — slot identity
        is what preserves user customizations across regens.
        """
        from datetime import timedelta

        plan_start = date.today() + timedelta(days=((7 - date.today().weekday()) % 7) or 7)
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Rename",
            start_date=plan_start,
            weeks=1,
            days_per_week=1,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        Workout.objects.create(
            tenant=self.tenant,
            plan=plan,
            date=plan_start,
            status="planned",
            category="strength",
            activity="Push",
            detail_json={
                "exercises": [
                    {"name": "Bench", "sets": [{"type": "weighted_reps", "reps": 5, "weight": 80}]},
                ]
            },
        )

        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{plan.id}/",
            {
                "schedule_json": {
                    "0": {"activity": "Pull", "category": "strength"},
                },
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        regen = Workout.objects.get(plan=plan, date=plan_start)
        self.assertEqual(regen.activity, "Pull")
        # detail_json was a user customization the new template doesn't
        # touch — slot identity preserves it across the rename. Improves
        # on the old DELETE+INSERT behavior where a rename clobbered it.
        self.assertEqual(
            regen.detail_json,
            {
                "exercises": [
                    {"name": "Bench", "sets": [{"type": "weighted_reps", "reps": 5, "weight": 80}]},
                ]
            },
        )
        self.assertIsNotNone(regen.slot_id)

    def test_delete_plan_preserves_done(self):
        """DELETE removes planned workouts but preserves completed ones."""
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Delete Me",
            start_date=date(2026, 4, 27),
            weeks=2,
            days_per_week=1,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        # One done, one planned
        Workout.objects.create(
            tenant=self.tenant,
            plan=plan,
            date=date(2026, 4, 27),
            status="done",
            category="strength",
            activity="Push",
        )
        Workout.objects.create(
            tenant=self.tenant,
            plan=plan,
            date=date(2026, 5, 4),
            status="planned",
            category="strength",
            activity="Push",
        )

        resp = self.client.delete(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{plan.id}/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 204)

        # Plan is gone
        self.assertFalse(WorkoutPlan.objects.filter(id=plan.id).exists())
        # Planned workout deleted, done workout preserved with plan=None
        remaining = Workout.objects.filter(tenant=self.tenant)
        self.assertEqual(remaining.count(), 1)
        self.assertEqual(remaining[0].status, "done")
        self.assertIsNone(remaining[0].plan)

    def test_summary_includes_active_plans(self):
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Active Plan",
            start_date=date(2026, 4, 27),
            weeks=4,
            days_per_week=3,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        Workout.objects.create(
            tenant=self.tenant,
            plan=plan,
            date=date(2026, 4, 27),
            status="planned",
            category="strength",
            activity="Push",
        )
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/summary/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("active_plans", resp.data)
        self.assertEqual(len(resp.data["active_plans"]), 1)
        self.assertEqual(resp.data["active_plans"][0]["name"], "Active Plan")
        self.assertEqual(resp.data["active_plans"][0]["workout_count"], 1)

    def test_profile_preferred_days_runtime(self):
        FuelProfile.objects.create(tenant=self.tenant)
        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/profile/",
            {"preferred_days": [0, 2, 4], "preferred_time": "morning"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["preferred_days"], [0, 2, 4])
        self.assertEqual(resp.data["preferred_time"], "morning")

    def test_create_plan_auth_required(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/",
            {"name": "No Auth", "weeks": 4, "days_per_week": 3, "schedule_json": {}},
            format="json",
        )
        self.assertEqual(resp.status_code, 401)


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class PlanReconcilerRaceTests(TestCase):
    """The deterministic race test the durable fix is supposed to win.

    Sequence:
    1. Tenant has a planned workout (uuid X) that the user is mid-editing.
    2. User holds an active edit-lock on uuid X.
    3. Assistant fires a plan PATCH that would (under the old code)
       DELETE+INSERT the workout into a fresh uuid.

    Under the new code:
    - The slot identity preserves uuid X across the regen.
    - The edit-lock blocks any field-level retemplate during the lock
      window.
    - The user's in-flight edits remain savable; no 404 on the browser
      side, no orphan-drafts dialog.
    """

    def setUp(self):
        from django.utils import timezone

        self.tenant = create_tenant(display_name="Race Test", telegram_chat_id=800300)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        self._tz_now = timezone.now

    def _make_plan_with_workout(self, schedule):
        """Create a plan + its workouts via the runtime API so slots are
        populated end-to-end (same code path production hits)."""
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/",
            {
                "name": "Race Plan",
                "start_date": (date.today() + timedelta(days=1)).isoformat(),
                "weeks": 2,
                "days_per_week": len(schedule),
                "schedule_json": schedule,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        return WorkoutPlan.objects.get(id=resp.data["id"])

    def test_uuid_survives_assistant_regen_with_unchanged_schedule(self):
        plan = self._make_plan_with_workout({"0": {"activity": "Push", "category": "strength"}})
        workouts_before = list(Workout.objects.filter(plan=plan).order_by("date"))
        uuids_before = {w.id for w in workouts_before}

        # Assistant fires PATCH with same schedule — equivalent to "no real change".
        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{plan.id}/",
            {"schedule_json": {"0": {"activity": "Push", "category": "strength"}}},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)

        workouts_after = list(Workout.objects.filter(plan=plan).order_by("date"))
        uuids_after = {w.id for w in workouts_after}
        self.assertEqual(
            uuids_before,
            uuids_after,
            "Workout uuids must survive a no-op regen — that's the property that fixes MJ's browser-mid-edit 404.",
        )

    def test_edit_lock_blocks_runtime_field_overwrite(self):
        plan = self._make_plan_with_workout({"0": {"activity": "Push", "category": "strength", "duration_minutes": 60}})
        workout = Workout.objects.filter(plan=plan).first()
        original_uuid = workout.id
        original_activity = workout.activity

        # User locks the workout (simulating mid-edit drawer).
        workout.edit_lock_until = self._tz_now() + timedelta(seconds=60)
        workout.edit_lock_owner = "user"
        workout.save(update_fields=["edit_lock_until", "edit_lock_owner"])

        # Assistant fires PATCH with a renamed activity + new duration.
        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{plan.id}/",
            {"schedule_json": {"0": {"activity": "Pull", "category": "strength", "duration_minutes": 90}}},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)

        # Same uuid — slot identity preserved.
        workout.refresh_from_db()
        self.assertEqual(workout.id, original_uuid)
        # Activity NOT overwritten because lock check skipped the retemplate.
        self.assertEqual(workout.activity, original_activity)
        # Same for duration.
        self.assertEqual(workout.duration_minutes, 60)

    def test_archived_slot_with_locked_workout_keeps_workout(self):
        plan = self._make_plan_with_workout(
            {
                "0": {"activity": "Push", "category": "strength"},
                "3": {"activity": "Pull", "category": "strength"},
            },
        )
        pull = Workout.objects.filter(plan=plan, activity="Pull").first()
        original_uuid = pull.id

        pull.edit_lock_until = self._tz_now() + timedelta(seconds=60)
        pull.edit_lock_owner = "user"
        pull.save(update_fields=["edit_lock_until", "edit_lock_owner"])

        # Assistant removes Pull from the schedule.
        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{plan.id}/",
            {"schedule_json": {"0": {"activity": "Push", "category": "strength"}}},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)

        # Pull workout still exists (lock prevented deletion).
        pull.refresh_from_db()
        self.assertEqual(pull.id, original_uuid)
        # Its slot got archived — orphan-for-audit pattern.
        self.assertIsNotNone(pull.slot.archived_at)


class CompletedWorkoutEditabilityTests(TestCase):
    """Phase 7 — completed workouts stay fully editable; the consumer PATCH
    endpoint stamps ``last_edited_by_user_at`` so the UI can footnote
    post-completion retouches.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Done Edit Test", telegram_chat_id=800400)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def _patch(self, w, data):
        return self.client.patch(f"/api/v1/fuel/workouts/{w.id}/", data, format="json")

    def test_patch_done_workout_succeeds_no_status_gate(self):
        from django.utils import timezone

        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            status="done",
            category="strength",
            activity="Push",
            duration_minutes=45,
        )
        # Backdate created_at so post-completion stamp fires.
        Workout.objects.filter(id=w.id).update(created_at=timezone.now() - timedelta(days=2))
        resp = self._patch(w, {"notes": "Felt strong"})
        self.assertEqual(resp.status_code, 200, msg=resp.data)
        w.refresh_from_db()
        self.assertEqual(w.notes, "Felt strong")
        self.assertEqual(w.status, "done")  # status preserved
        self.assertIsNotNone(w.last_edited_by_user_at)

    def test_patch_done_within_24h_does_not_stamp_footnote(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            status="done",
            category="strength",
            activity="Push",
        )
        resp = self._patch(w, {"notes": "Same-day amendment"})
        self.assertEqual(resp.status_code, 200)
        w.refresh_from_db()
        # Same-day edits don't surface a footnote — only retouches that
        # land >24h after creation indicate "edited after the fact".
        self.assertIsNone(w.last_edited_by_user_at)

    def test_patch_planned_workout_does_not_stamp_footnote(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            status="planned",
            category="strength",
            activity="Push",
        )
        resp = self._patch(w, {"notes": "Adjustment"})
        self.assertEqual(resp.status_code, 200)
        w.refresh_from_db()
        self.assertIsNone(w.last_edited_by_user_at)


class TenantFuelVersionTests(TestCase):
    """Phase 6 — every Workout/WorkoutPlan write bumps tenant.fuel_version,
    and ``GET /api/v1/fuel/version/`` surfaces the counter for the
    frontend's stale-edit-warning pill.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Fuel Version", telegram_chat_id=800401)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_workout_save_bumps_fuel_version(self):
        self.tenant.refresh_from_db()
        before = self.tenant.fuel_version
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        self.tenant.refresh_from_db()
        self.assertGreater(self.tenant.fuel_version, before)

    def test_workout_delete_bumps_fuel_version(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        self.tenant.refresh_from_db()
        before = self.tenant.fuel_version
        w.delete()
        self.tenant.refresh_from_db()
        self.assertGreater(self.tenant.fuel_version, before)

    def test_plan_save_bumps_fuel_version(self):
        self.tenant.refresh_from_db()
        before = self.tenant.fuel_version
        WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Bump",
            start_date=date(2026, 6, 1),
            weeks=1,
            days_per_week=1,
        )
        self.tenant.refresh_from_db()
        self.assertGreater(self.tenant.fuel_version, before)

    def test_version_endpoint_returns_current_counter(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        resp = self.client.get("/api/v1/fuel/version/")
        self.assertEqual(resp.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertEqual(resp.data["fuel_version"], self.tenant.fuel_version)


class ConsumerWorkoutPlanTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Consumer Plan Test", telegram_chat_id=800062)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_list_plans(self):
        WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Plan A",
            start_date=date(2026, 4, 27),
            weeks=4,
            days_per_week=3,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        resp = self.client.get("/api/v1/fuel/plans/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["name"], "Plan A")

    def test_create_plan(self):
        resp = self.client.post(
            "/api/v1/fuel/plans/",
            {
                "name": "New Plan",
                "start_date": "2026-04-27",
                "weeks": 4,
                "days_per_week": 3,
                "schedule_json": {"0": {"activity": "Push", "category": "strength"}},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["name"], "New Plan")

    def test_get_plan_detail(self):
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Detail Plan",
            start_date=date(2026, 4, 27),
            weeks=4,
            days_per_week=3,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        resp = self.client.get(f"/api/v1/fuel/plans/{plan.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["name"], "Detail Plan")

    def test_patch_plan(self):
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Old Name",
            start_date=date(2026, 4, 27),
            weeks=4,
            days_per_week=3,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        resp = self.client.patch(
            f"/api/v1/fuel/plans/{plan.id}/",
            {"name": "New Name", "status": "paused"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["name"], "New Name")
        self.assertEqual(resp.data["status"], "paused")

    def test_delete_plan(self):
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Delete Me",
            start_date=date(2026, 4, 27),
            weeks=4,
            days_per_week=3,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        Workout.objects.create(
            tenant=self.tenant,
            plan=plan,
            date=date(2026, 4, 27),
            status="planned",
            category="strength",
            activity="Push",
        )
        resp = self.client.delete(f"/api/v1/fuel/plans/{plan.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(WorkoutPlan.objects.filter(id=plan.id).exists())
        # Planned workout deleted
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 0)

    def test_tenant_isolation(self):
        other = create_tenant(display_name="Other", telegram_chat_id=800099)
        WorkoutPlan.objects.create(
            tenant=other,
            name="Other Plan",
            start_date=date(2026, 4, 27),
            weeks=4,
            days_per_week=3,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        resp = self.client.get("/api/v1/fuel/plans/")
        self.assertEqual(len(resp.data), 0)

    def test_workout_serializer_includes_plan(self):
        """Workout serializer includes plan_id and plan_name."""
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="My Plan",
            start_date=date(2026, 4, 27),
            weeks=1,
            days_per_week=1,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )
        Workout.objects.create(
            tenant=self.tenant,
            plan=plan,
            date=date(2026, 4, 27),
            status="planned",
            category="strength",
            activity="Push",
        )
        resp = self.client.get("/api/v1/fuel/workouts/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["plan_id"], str(plan.id))
        self.assertEqual(resp.data[0]["plan_name"], "My Plan")


# ═════════════════════════════════════════════════════════════════════
# Phase 0 — shape-agnostic set-metric accessor (#593)
# ═════════════════════════════════════════════════════════════════════


class SetMetricTests(UnitTestCase):
    """`set_metric` / `coerce_set` — pure, no DB."""

    def test_explicit_valid_type_wins(self):
        from .set_contract import set_metric

        self.assertEqual(set_metric({"type": "hold_time", "weight": 99}), "hold_time")
        self.assertEqual(set_metric({"type": "weighted_reps"}), "weighted_reps")
        self.assertEqual(set_metric({"type": "bodyweight_reps"}), "bodyweight_reps")

    def test_invalid_type_falls_through_to_inference(self):
        from .set_contract import set_metric

        # distance_time is not a *set* metric → ignore, infer from fields.
        self.assertEqual(set_metric({"type": "distance_time", "reps": 8, "weight": 60}), "weighted_reps")
        self.assertEqual(set_metric({"type": "garbage"}), "bodyweight_reps")
        self.assertEqual(set_metric({"type": None, "hold_s": 30}), "hold_time")

    def test_field_presence_inference_matches_legacy_sniff(self):
        from .set_contract import set_metric

        self.assertEqual(set_metric({"hold_s": 60}), "hold_time")
        self.assertEqual(set_metric({"hold_s": 0}), "hold_time")  # presence, not truthiness
        self.assertEqual(set_metric({"reps": 8, "weight": 75}), "weighted_reps")
        self.assertEqual(set_metric({"reps": 12, "weight": 0}), "bodyweight_reps")  # 0 = bodyweight
        self.assertEqual(set_metric({"reps": 10}), "bodyweight_reps")
        # hold_s is checked before weight — matches the historical order.
        self.assertEqual(set_metric({"hold_s": 45, "weight": 20}), "hold_time")

    def test_registry_refine_only_when_fields_inconclusive(self):
        from .set_contract import set_metric

        self.assertEqual(set_metric({}, exercise_name="plank"), "hold_time")
        self.assertEqual(set_metric({}, exercise_name="Bench Press"), "weighted_reps")
        self.assertEqual(set_metric({}, exercise_name="pull-up"), "bodyweight_reps")
        self.assertEqual(set_metric({}, exercise_name="not a real move"), "bodyweight_reps")
        # Fields still beat the registry when present.
        self.assertEqual(set_metric({"hold_s": 30}, exercise_name="Bench Press"), "hold_time")

    def test_non_dict_degrades_safely(self):
        from .set_contract import set_metric

        for bad in (None, "x", 7, [], ("a",)):
            self.assertEqual(set_metric(bad), "bodyweight_reps")

    def test_coerce_set_stamps_and_is_idempotent(self):
        from .set_contract import coerce_set

        once = coerce_set({"reps": 8, "weight": 75})
        self.assertEqual(once, {"reps": 8, "weight": 75, "type": "weighted_reps"})
        self.assertEqual(coerce_set(once), once)  # idempotent
        self.assertEqual(coerce_set(None), {"type": "bodyweight_reps"})
        # Original is not mutated.
        src = {"hold_s": 60}
        coerce_set(src)
        self.assertNotIn("type", src)


class CalisthenicsAggregateRegressionTests(TestCase):
    """Proves Phase 0 routing through `set_metric` is behaviour-neutral
    on legacy flat data, and that typed data resolves identically."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Calis Test", telegram_chat_id=800077)

    def _workout(self, detail):
        return Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 5, 1),
            category="calisthenics",
            activity="Skill work",
            detail_json=detail,
        )

    def test_legacy_flat_shape_unchanged(self):
        from .services import aggregate_calisthenics_progress

        w = self._workout(
            {
                "skills": [
                    {"name": "Plank", "sets": [{"hold_s": 60}, {"hold_s": 75}]},
                    {"name": "Pull-up", "sets": [{"reps": 8}, {"reps": 10}]},
                ]
            }
        )
        out = aggregate_calisthenics_progress([w])
        self.assertEqual(
            out,
            {
                "Plank": {"points": [{"date": "2026-05-01", "value": 75}], "is_hold": True},
                "Pull-up": {"points": [{"date": "2026-05-01", "value": 10}], "is_hold": False},
            },
        )

    def test_typed_shape_resolves_identically(self):
        from .services import aggregate_calisthenics_progress

        w = self._workout(
            {
                "skills": [
                    {"name": "Plank", "sets": [{"type": "hold_time", "hold_s": 90}]},
                    {"name": "Dip", "sets": [{"type": "bodyweight_reps", "reps": 12}]},
                ]
            }
        )
        out = aggregate_calisthenics_progress([w])
        self.assertTrue(out["Plank"]["is_hold"])
        self.assertEqual(out["Plank"]["points"][0]["value"], 90)
        self.assertFalse(out["Dip"]["is_hold"])
        self.assertEqual(out["Dip"]["points"][0]["value"], 12)


# ═════════════════════════════════════════════════════════════════════
# Phase 1 — deterministic registry override at write paths (#593)
# ═════════════════════════════════════════════════════════════════════


class NormalizeDetailTests(UnitTestCase):
    """`normalize_detail` — pure registry correction, no DB."""

    def test_plank_miscategorized_is_corrected(self):
        from .set_contract import normalize_detail

        detail, cat, ov = normalize_detail(
            {"exercises": [{"name": "Plank", "sets": [{"reps": 8, "weight": 75}]}]},
            "strength",
        )
        self.assertEqual(cat, "calisthenics")
        self.assertEqual(detail["exercises"][0]["sets"][0]["type"], "hold_time")
        kinds = {o.get("field") for o in ov}
        self.assertEqual(kinds, {"set.type", "category"})
        self.assertIn("_normalized", detail)

    def test_weighted_pullups_promoted_to_strength(self):
        from .set_contract import normalize_detail

        detail, cat, _ = normalize_detail(
            {"exercises": [{"name": "weighted pull-ups", "sets": [{"reps": 5, "weight": 20}]}]},
            "calisthenics",
        )
        self.assertEqual(cat, "strength")
        self.assertEqual(detail["exercises"][0]["sets"][0]["type"], "weighted_reps")

    def test_unknown_exercise_left_untouched(self):
        from .set_contract import normalize_detail

        src = {"exercises": [{"name": "Zercher Zottman Thing", "sets": [{"reps": 8}]}]}
        detail, cat, ov = normalize_detail(src, "strength")
        self.assertEqual(cat, "strength")
        self.assertNotIn("type", detail["exercises"][0]["sets"][0])
        self.assertEqual(ov, [])
        self.assertNotIn("_normalized", detail)

    def test_skills_container_handled(self):
        from .set_contract import normalize_detail

        detail, cat, ov = normalize_detail({"skills": [{"name": "Plank", "sets": [{"hold_s": 60}]}]}, "calisthenics")
        self.assertEqual(cat, "calisthenics")
        self.assertEqual(detail["skills"][0]["sets"][0]["type"], "hold_time")
        self.assertEqual(ov, [])  # already correct → no override note

    def test_cardio_category_never_touched(self):
        from .set_contract import normalize_detail

        detail, cat, _ = normalize_detail(
            {"exercises": [{"name": "Bench Press", "sets": [{"reps": 5, "weight": 100}]}]},
            "cardio",
        )
        self.assertEqual(cat, "cardio")  # only strength↔calisthenics flips
        self.assertEqual(detail["exercises"][0]["sets"][0]["type"], "weighted_reps")

    def test_non_dict_and_input_not_mutated(self):
        from .set_contract import normalize_detail

        self.assertEqual(normalize_detail("x", "strength"), ("x", "strength", []))
        self.assertEqual(normalize_detail(None, "strength"), (None, "strength", []))
        src = {"exercises": [{"name": "Plank", "sets": [{"reps": 8, "weight": 75}]}]}
        normalize_detail(src, "strength")
        self.assertNotIn("type", src["exercises"][0]["sets"][0])
        self.assertNotIn("_normalized", src)

    def test_idempotent(self):
        from .set_contract import normalize_detail

        d1, c1, _ = normalize_detail(
            {"exercises": [{"name": "Plank", "sets": [{"reps": 8, "weight": 75}]}]},
            "strength",
        )
        d2, c2, ov2 = normalize_detail(d1, c1)
        self.assertEqual((d2, c2), (d1, c1))
        self.assertEqual(ov2, [])


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeNormalizeTests(TestCase):
    """End-to-end: the canonical bug fixed through the real HTTP path."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Norm Test", telegram_chat_id=800088)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_runtime_plank_as_reps_weight_self_corrects(self):
        # Registry knows "plank" is a hold; the payload has no duration —
        # genuinely incomplete data → 400 self-correct, nothing stored.
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {
                "date": "2026-05-01",
                "category": "strength",
                "activity": "Plank",
                "detail_json": {"exercises": [{"name": "Plank", "sets": [{"reps": 8, "weight": 75}]}]},
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "validation_failed")
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 0)

    def test_runtime_plank_with_duration_reclassified_and_stored(self):
        # Coherent hold (has hold_s) logged under the wrong category →
        # silently corrected to calisthenics/hold_time and stored.
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {
                "date": "2026-05-01",
                "category": "strength",
                "activity": "Plank",
                "detail_json": {"exercises": [{"name": "Plank", "sets": [{"hold_s": 60}]}]},
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        w = Workout.objects.get(tenant=self.tenant)
        self.assertEqual(w.category, "calisthenics")
        self.assertEqual(w.detail_json["exercises"][0]["sets"][0]["type"], "hold_time")
        self.assertIn("_normalized", w.detail_json)

    def test_runtime_correct_data_not_falsely_flipped(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {
                "date": "2026-05-01",
                "category": "strength",
                "activity": "Bench Press",
                "detail_json": {"exercises": [{"name": "Bench Press", "sets": [{"reps": 5, "weight": 100}]}]},
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        w = Workout.objects.get(tenant=self.tenant)
        self.assertEqual(w.category, "strength")
        self.assertEqual(w.detail_json["exercises"][0]["sets"][0]["type"], "weighted_reps")
        self.assertNotIn("_normalized", w.detail_json)

    def test_runtime_patch_corrects(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 5, 1),
            category="strength",
            activity="Mystery",
            detail_json={},
        )
        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/workouts/{w.id}/",
            {"detail_json": {"exercises": [{"name": "Plank", "sets": [{"hold_s": 45}]}]}},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        w.refresh_from_db()
        self.assertEqual(w.category, "calisthenics")
        self.assertEqual(w.detail_json["exercises"][0]["sets"][0]["type"], "hold_time")


# ═════════════════════════════════════════════════════════════════════
# Phase 2 — typed discriminated-union contract + self-correct (#593)
# ═════════════════════════════════════════════════════════════════════


class ValidateDetailTests(UnitTestCase):
    """`validate_detail` — coerce + Pydantic enforcement, no DB."""

    def test_valid_weighted_passes(self):
        from .set_contract import validate_detail

        d, err = validate_detail(
            {"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 75}]}]},
            "strength",
        )
        self.assertIsNone(err)
        self.assertEqual(d["exercises"][0]["sets"][0]["type"], "weighted_reps")

    def test_legacy_shapes_coerced_then_valid(self):
        from .set_contract import validate_detail

        for raw, expected in (
            ({"hold_s": 60}, "hold_time"),
            ({"reps": 12}, "bodyweight_reps"),
            ({"reps": 12, "weight": 0}, "bodyweight_reps"),
            ({"reps": 5, "weight": 100}, "weighted_reps"),
        ):
            d, err = validate_detail({"exercises": [{"name": "X", "sets": [raw]}]}, "calisthenics")
            self.assertIsNone(err, raw)
            self.assertEqual(d["exercises"][0]["sets"][0]["type"], expected, raw)

    def test_incoherent_is_rejected_with_loc(self):
        from .set_contract import validate_detail

        _, err = validate_detail(
            {"exercises": [{"name": "Iso", "sets": [{"type": "hold_time"}]}]},
            "strength",
        )
        self.assertIsNotNone(err)
        body = err.as_tool_result()
        self.assertEqual(body["error"], "validation_failed")
        flat = {(tuple(x["loc"]), x["type"]) for x in body["details"]}
        self.assertTrue(any("hold_s" in loc and t == "missing" for loc, t in flat))

    def test_weighted_missing_weight_rejected(self):
        from .set_contract import validate_detail

        _, err = validate_detail(
            {"exercises": [{"name": "S", "sets": [{"type": "weighted_reps", "reps": 5}]}]},
            "strength",
        )
        self.assertIsNotNone(err)

    def test_non_numeric_rejected(self):
        from .set_contract import validate_detail

        _, err = validate_detail(
            {"exercises": [{"name": "S", "sets": [{"type": "bodyweight_reps", "reps": "lots"}]}]},
            "strength",
        )
        self.assertIsNotNone(err)

    def test_cardio_is_passthrough(self):
        from .set_contract import validate_detail

        payload = {"distance_km": 5, "pace": "5:30"}
        self.assertEqual(validate_detail(payload, "cardio"), (payload, None))

    def test_extras_preserved_and_valid(self):
        from .set_contract import validate_detail

        d, err = validate_detail(
            {
                "_normalized": [{"x": 1}],
                "exercises": [{"name": "Sq", "sets": [{"reps": 5, "weight": 100, "est_1rm": 116.7, "pr": True}]}],
            },
            "strength",
        )
        self.assertIsNone(err)
        s = d["exercises"][0]["sets"][0]
        self.assertEqual(s["est_1rm"], 116.7)
        self.assertTrue(s["pr"])
        self.assertIn("_normalized", d)

    def test_skills_container_validated(self):
        from .set_contract import validate_detail

        _, err = validate_detail({"skills": [{"name": "P", "sets": [{"type": "hold_time"}]}]}, "calisthenics")
        self.assertIsNotNone(err)


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeValidateTests(TestCase):
    """End-to-end: incoherent payloads get a 400 self-correct envelope."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Val Test", telegram_chat_id=800099)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_runtime_rejects_incoherent(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {
                "date": "2026-05-01",
                "category": "strength",
                "activity": "Custom Iso Hold",
                "detail_json": {"exercises": [{"name": "Custom Iso Hold", "sets": [{"type": "hold_time"}]}]},
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "validation_failed")
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 0)

    def test_runtime_accepts_coherent(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {
                "date": "2026-05-01",
                "category": "strength",
                "activity": "Bench Press",
                "detail_json": {"exercises": [{"name": "Bench Press", "sets": [{"reps": 5, "weight": 100}]}]},
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        w = Workout.objects.get(tenant=self.tenant)
        self.assertEqual(w.detail_json["exercises"][0]["sets"][0]["type"], "weighted_reps")

    def test_runtime_patch_rejects_incoherent(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 5, 1),
            category="strength",
            activity="Custom",
            detail_json={
                "exercises": [{"name": "Custom", "sets": [{"reps": 5, "weight": 60, "type": "weighted_reps"}]}]
            },
        )
        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/workouts/{w.id}/",
            {"detail_json": {"exercises": [{"name": "Custom", "sets": [{"type": "weighted_reps", "reps": 5}]}]}},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 400)
        w.refresh_from_db()
        self.assertEqual(w.detail_json["exercises"][0]["sets"][0]["weight"], 60)


class SerializerValidateTests(TestCase):
    """Frontend-origin path rejects incoherent detail_json too."""

    def setUp(self):
        self.tenant = create_tenant(display_name="SerVal", telegram_chat_id=800100)

    def _ser(self, detail):
        from .serializers import WorkoutSerializer

        return WorkoutSerializer(
            data={
                "date": "2026-05-01",
                "category": "strength",
                "activity": "Custom",
                "detail_json": detail,
            },
            context={"tenant": self.tenant},
        )

    def test_incoherent_rejected(self):
        s = self._ser({"exercises": [{"name": "Custom", "sets": [{"type": "hold_time"}]}]})
        self.assertFalse(s.is_valid())
        self.assertIn("detail_json", s.errors)

    def test_coherent_accepted(self):
        s = self._ser({"exercises": [{"name": "Custom", "sets": [{"reps": 8, "weight": 60}]}]})
        self.assertTrue(s.is_valid(), s.errors)


# ═════════════════════════════════════════════════════════════════════
# Phase 4 — backfill migration + read-only dry-run (#593)
# ═════════════════════════════════════════════════════════════════════


class StampSetTypeMigrationTests(TestCase):
    """Migration 0010 forward fn — stamps type, idempotent, non-destructive."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Mig Test", telegram_chat_id=800111)

    def _run(self):
        import importlib

        from django.apps import apps as django_apps

        mig = importlib.import_module("apps.fuel.migrations.0010_stamp_set_type")
        mig.stamp_set_types(django_apps, None)

    def test_stamps_workout_and_template_idempotently(self):
        from .models import WorkoutTemplate

        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 5, 1),
            category="strength",
            activity="Bench",
            detail_json={"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 75, "est_1rm": 116.7}]}]},
        )
        tmpl = WorkoutTemplate.objects.create(
            tenant=self.tenant,
            name="Core",
            category="calisthenics",
            activity="Plank",
            detail_json={"skills": [{"name": "Plank", "sets": [{"hold_s": 60}]}]},
        )
        cardio = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 5, 2),
            category="cardio",
            activity="Run",
            detail_json={"distance_km": 5, "pace": "5:30"},
        )

        self._run()
        w.refresh_from_db()
        tmpl.refresh_from_db()
        cardio.refresh_from_db()

        s = w.detail_json["exercises"][0]["sets"][0]
        self.assertEqual(s["type"], "weighted_reps")
        self.assertEqual(s["est_1rm"], 116.7)  # extras preserved
        self.assertEqual(tmpl.detail_json["skills"][0]["sets"][0]["type"], "hold_time")
        self.assertEqual(cardio.detail_json, {"distance_km": 5, "pace": "5:30"})  # untouched

        snapshot = w.detail_json
        self._run()  # idempotent
        w.refresh_from_db()
        self.assertEqual(w.detail_json, snapshot)

    def test_non_dict_detail_is_skipped(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 5, 3),
            category="other",
            activity="x",
            detail_json=[],
        )
        self._run()  # must not raise
        w.refresh_from_db()
        self.assertEqual(w.detail_json, [])


class DryRunCommandTests(TestCase):
    """The dry-run command reports and writes nothing."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Dry Test", telegram_chat_id=800122)

    def test_reports_and_does_not_mutate(self):
        from io import StringIO

        from django.core.management import call_command

        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 5, 1),
            category="strength",
            activity="Bench",
            detail_json={"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 75}]}]},
        )
        before = dict(w.detail_json)
        out = StringIO()
        call_command("fuel_set_type_dryrun", stdout=out)
        text = out.getvalue()
        self.assertIn("DRY RUN", text)
        self.assertIn("Workout:", text)
        self.assertIn("schedule_json", text)
        w.refresh_from_db()
        self.assertEqual(w.detail_json, before)  # untouched — read-only
        self.assertNotIn("type", w.detail_json["exercises"][0]["sets"][0])


class ScheduleWindowTimezoneTests(TestCase):
    """The rolling schedule window (?window=Nd) must anchor on the tenant's
    local 'today', not the server's UTC date — otherwise a workout on the
    user's current day can fall outside the window near the UTC boundary
    (e.g. a JST evening, where server-UTC is still 'yesterday')."""

    def setUp(self):
        self.tenant = create_tenant(display_name="TZ Test", telegram_chat_id=800042)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_schedule_window_anchors_on_tenant_today(self):
        # Tenant-local today = 2026-04-21 → window [2026-04-21, 2026-04-28].
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="In Window",
            status="planned",
        )
        # Past the window's upper bound — must not appear.
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 30),
            category="cardio",
            activity="Out Of Window",
            status="planned",
        )
        # Patch the tz resolver so the test is independent of the real clock.
        # That it is called with the tenant (not date.today()) is the fix.
        with patch("apps.fuel.views.today_in_tenant_tz", return_value=date(2026, 4, 21)) as mock_today:
            resp = self.client.get("/api/v1/fuel/workouts/?window=7d")

        self.assertEqual(resp.status_code, 200)
        mock_today.assert_called_once_with(self.tenant)
        dates = {w["date"] for w in resp.data}
        # Present because it's the tenant's today; the pre-fix UTC anchor
        # (real today is months away) would have excluded it entirely.
        self.assertIn("2026-04-21", dates)
        self.assertNotIn("2026-04-30", dates)


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeWorkoutPlanCreateTests(TestCase):
    """Structured plan creation: tenant-tz dates, validation, intensity,
    idempotency, per-week progression. The agent passes a weekday cadence;
    the backend deterministically materializes the calendar."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Plan Create", telegram_chat_id=800300)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        # The fuel cron lifecycle hits the OpenClaw gateway — stub it out so
        # plan-create tests don't reach the network.
        cron_patch = patch("apps.fuel.runtime_views._manage_fuel_cron", return_value=None)
        cron_patch.start()
        self.addCleanup(cron_patch.stop)

    def _url(self):
        return f"/api/v1/fuel/runtime/{self.tenant.id}/plans/"

    def _post(self, body):
        return self.client.post(self._url(), data=body, format="json", **self.headers)

    def test_explicit_start_date_materializes_correct_weekday_dates(self):
        # Start Monday 2026-06-15; Mon/Wed/Fri cadence over 2 weeks.
        resp = self._post(
            {
                "name": "Strength Builder",
                "weeks": 2,
                "days_per_week": 3,
                "start_date": "2026-06-15",
                "schedule_json": {
                    "0": {"category": "strength", "activity": "Upper Pull"},
                    "2": {
                        "category": "cardio",
                        "activity": "Tempo Run",
                        "detail_json": {"distance_km": 5, "pace": "5:30"},
                    },
                    "4": {"category": "strength", "activity": "Lower Power"},
                },
            }
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        plan = WorkoutPlan.objects.get(id=resp.data["id"])
        workouts = list(Workout.objects.filter(plan=plan).order_by("date"))
        self.assertEqual(len(workouts), 6)
        self.assertEqual([str(w.date) for w in workouts[:3]], ["2026-06-15", "2026-06-17", "2026-06-19"])
        # Weekday labels match the real calendar (the bug the user reported).
        self.assertEqual([w.date.weekday() for w in workouts[:3]], [0, 2, 4])

    @patch("apps.fuel.runtime_views.today_in_tenant_tz")
    def test_default_start_date_uses_tenant_tz_next_monday(self, mock_today):
        # Tenant-local today is Monday 2026-06-08 -> next Monday is 06-15.
        # The pre-fix bare date.today() would drift a day in the evening.
        mock_today.return_value = date(2026, 6, 8)
        resp = self._post(
            {
                "name": "Default Start",
                "weeks": 1,
                "days_per_week": 1,
                "schedule_json": {"0": {"category": "strength", "activity": "Full Body"}},
            }
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["start_date"], "2026-06-15")
        mock_today.assert_called_with(self.tenant)

    def test_target_rpe_persists_as_workout_rpe(self):
        resp = self._post(
            {
                "name": "Intensity Plan",
                "weeks": 1,
                "days_per_week": 1,
                "start_date": "2026-06-15",
                "schedule_json": {"0": {"category": "strength", "activity": "Squats", "target_rpe": 8}},
            }
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        w = Workout.objects.get(plan_id=resp.data["id"])
        self.assertEqual(w.rpe, 8)

    def test_malformed_strength_detail_rejected_before_persist(self):
        resp = self._post(
            {
                "name": "Bad Detail",
                "weeks": 1,
                "days_per_week": 1,
                "start_date": "2026-06-15",
                "schedule_json": {
                    "0": {
                        "category": "strength",
                        "activity": "Bench",
                        "detail_json": {
                            "exercises": [{"name": "Bench", "sets": [{"type": "weighted_reps", "reps": "lots"}]}]
                        },
                    }
                },
            }
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(WorkoutPlan.objects.filter(tenant=self.tenant, name="Bad Detail").count(), 0)
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 0)

    def test_invalid_weekday_key_rejected(self):
        resp = self._post(
            {
                "name": "Bad Day",
                "weeks": 1,
                "days_per_week": 1,
                "start_date": "2026-06-15",
                "schedule_json": {"9": {"category": "strength", "activity": "X"}},
            }
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(WorkoutPlan.objects.filter(tenant=self.tenant, name="Bad Day").count(), 0)

    def test_idempotent_double_create_returns_200_no_duplicate(self):
        body = {
            "name": "Dedup Plan",
            "weeks": 1,
            "days_per_week": 1,
            "start_date": "2026-06-15",
            "schedule_json": {"0": {"category": "strength", "activity": "Squats"}},
        }
        r1 = self._post(body)
        self.assertEqual(r1.status_code, 201, r1.data)
        r2 = self._post(body)
        self.assertEqual(r2.status_code, 200, r2.data)
        self.assertTrue(r2.data.get("deduped"))
        self.assertEqual(WorkoutPlan.objects.filter(tenant=self.tenant, name="Dedup Plan").count(), 1)
        self.assertEqual(Workout.objects.filter(plan_id=r1.data["id"]).count(), 1)

    def test_week_overrides_progression_and_rest(self):
        resp = self._post(
            {
                "name": "Periodized",
                "weeks": 2,
                "days_per_week": 2,
                "start_date": "2026-06-15",
                "schedule_json": {
                    "0": {"category": "strength", "activity": "Heavy Squats", "target_rpe": 9},
                    "2": {"category": "strength", "activity": "Heavy Bench", "target_rpe": 9},
                },
                "week_overrides": {
                    "1": {
                        "0": {"category": "strength", "activity": "Deload Squats", "target_rpe": 5},
                        "2": None,
                    }
                },
            }
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        plan = WorkoutPlan.objects.get(id=resp.data["id"])
        dates = [str(w.date) for w in Workout.objects.filter(plan=plan).order_by("date")]
        # Week 1: Mon 06-15 + Wed 06-17. Week 2: only Mon 06-22 (Wed rested out).
        self.assertEqual(dates, ["2026-06-15", "2026-06-17", "2026-06-22"])
        deload = Workout.objects.get(plan=plan, date=date(2026, 6, 22))
        self.assertEqual(deload.activity, "Deload Squats")
        self.assertEqual(deload.rpe, 5)

    def test_objective_persisted_and_serialized(self):
        resp = self._post(
            {
                "name": "Goal Plan",
                "weeks": 1,
                "days_per_week": 1,
                "start_date": "2026-06-15",
                "objective": "Build pull strength",
                "schedule_json": {"0": {"category": "strength", "activity": "Pull-ups"}},
            }
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["objective"], "Build pull strength")
        self.assertEqual(WorkoutPlan.objects.get(id=resp.data["id"]).objective, "Build pull strength")


# ═════════════════════════════════════════════════════════════════════
# HealthKit sync endpoint (POST /api/v1/fuel/healthkit/sync/)
# ═════════════════════════════════════════════════════════════════════


class HealthKitSyncTests(TestCase):
    """Idempotent ingest, planned-workout auto-complete gates, daily
    metric upserts, tombstones, tz self-heal, and the one-push contract."""

    maxDiff = None

    def setUp(self):
        self.tenant = create_tenant(display_name="HK Test", telegram_chat_id=800042)
        self.tenant.fuel_enabled = True
        self.tenant.save(update_fields=["fuel_enabled"])
        self.user = self.tenant.user
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        FuelProfile.objects.create(tenant=self.tenant)
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def _post(self, payload):
        return self.client.post("/api/v1/fuel/healthkit/sync/", payload, format="json")

    @staticmethod
    def _workout_item(**overrides):
        item = {
            "external_id": "hk-uuid-0001",
            "activity": "Outdoor Run",
            "category": "cardio",
            "raw_type": "running",
            "source_bundle": "com.apple.health.workout",
            # 07:30 JST on 2026-06-10
            "started_at": "2026-06-09T22:30:00Z",
            "ended_at": "2026-06-09T23:12:00Z",
            "duration_minutes": 42,
            "metrics": {"distance_km": 5.214, "avg_hr": 152, "peak_hr": 171, "calories": 380, "elevation_m": 40},
        }
        item.update(overrides)
        return item

    # ── gates ──────────────────────────────────────────────────────────

    def test_fuel_disabled_409(self):
        self.tenant.fuel_enabled = False
        self.tenant.save(update_fields=["fuel_enabled"])
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"], "fuel_disabled")

    def test_suspended_403(self):
        from apps.tenants.models import Tenant

        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.save(update_fields=["status"])
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data["error"], "suspended")

    def test_empty_payload_400(self):
        resp = self._post({})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "empty_payload")

    def test_too_many_workouts_400(self):
        items = [self._workout_item(external_id=f"hk-{i}") for i in range(51)]
        resp = self._post({"workouts": items})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "too_many_workouts")

    def test_throttle_configured(self):
        from .views import HealthKitSyncView

        self.assertTrue(HealthKitSyncView.throttle_classes)

    # ── standalone create + idempotency ────────────────────────────────

    def test_creates_standalone_workout_with_normalized_metrics(self):
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["results"][0]["status"], "created")
        w = Workout.objects.get(tenant=self.tenant, external_id="hk-uuid-0001")
        self.assertEqual(w.source, "healthkit")
        self.assertEqual(w.status, "done")
        # 22:30Z = 07:30 JST next day — date buckets in tenant tz
        self.assertEqual(w.date, date(2026, 6, 10))
        self.assertEqual(w.detail_json["distance_km"], 5.21)
        self.assertEqual(w.detail_json["avg_hr"], 152)
        self.assertEqual(w.detail_json["elevation"], 40)  # elevation_m → elevation
        self.assertNotIn("elevation_m", w.detail_json)
        self.assertEqual(w.detail_json["pace"], "8:03")  # 42min / 5.21km
        self.assertEqual(w.detail_json["_healthkit"]["raw_type"], "running")
        self.assertFalse(w.detail_json["_healthkit"]["matched"])

    def test_resync_is_duplicate_and_preserves_user_edits(self):
        self._post({"workouts": [self._workout_item()]})
        w = Workout.objects.get(tenant=self.tenant, external_id="hk-uuid-0001")
        w.duration_minutes = 99  # user edit between syncs
        w.save(update_fields=["duration_minutes"])
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "duplicate")
        self.assertEqual(Workout.objects.filter(tenant=self.tenant, external_id="hk-uuid-0001").count(), 1)
        w.refresh_from_db()
        self.assertEqual(w.duration_minutes, 99)

    def test_within_batch_duplicate(self):
        resp = self._post({"workouts": [self._workout_item(), self._workout_item()]})
        statuses = sorted(r["status"] for r in resp.data["results"])
        self.assertEqual(statuses, ["created", "duplicate"])
        self.assertEqual(Workout.objects.filter(tenant=self.tenant, external_id="hk-uuid-0001").count(), 1)

    def test_invalid_item_skips_bad_continues_good(self):
        bad = self._workout_item(external_id="")
        good = self._workout_item(external_id="hk-good")
        resp = self._post({"workouts": [bad, good]})
        self.assertEqual(resp.data["results"][0]["status"], "error")
        self.assertEqual(resp.data["results"][1]["status"], "created")
        self.assertEqual(resp.data["summary"]["errors"], 1)
        self.assertEqual(resp.data["summary"]["created"], 1)

    def test_duration_out_of_range_rejected(self):
        resp = self._post({"workouts": [self._workout_item(duration_minutes=0)]})
        self.assertEqual(resp.data["results"][0]["status"], "error")
        resp = self._post({"workouts": [self._workout_item(duration_minutes=2000)]})
        self.assertEqual(resp.data["results"][0]["status"], "error")

    # ── planned-workout auto-complete ──────────────────────────────────

    def _planned(self, **overrides):
        defaults = dict(
            tenant=self.tenant,
            date=date(2026, 6, 10),
            status="planned",
            category="cardio",
            activity="Morning 10K Run",
            duration_minutes=45,
            source="assistant",
        )
        defaults.update(overrides)
        return Workout.objects.create(**defaults)

    def test_matches_day_only_planned_session(self):
        planned = self._planned()
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "matched_planned")
        planned.refresh_from_db()
        self.assertEqual(planned.status, "done")
        self.assertEqual(planned.external_id, "hk-uuid-0001")
        self.assertEqual(planned.duration_minutes, 42)
        self.assertEqual(planned.activity, "Morning 10K Run")  # plan name kept
        self.assertTrue(planned.detail_json["_healthkit"]["matched"])
        self.assertEqual(planned.detail_json["avg_hr"], 152)
        self.assertEqual(planned.version, 1)
        self.assertEqual(planned.notes_thread[-1]["who"], "system")
        self.assertIn("Apple Health", planned.notes_thread[-1]["text"])
        # No second row created
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 1)

    def test_walk_does_not_complete_planned_run(self):
        planned = self._planned()
        walk = self._workout_item(external_id="hk-walk", activity="Walk", raw_type="walking", duration_minutes=50)
        resp = self._post({"workouts": [walk]})
        self.assertEqual(resp.data["results"][0]["status"], "created")
        planned.refresh_from_db()
        self.assertEqual(planned.status, "planned")

    def test_walk_completes_planned_walk(self):
        planned = self._planned(activity="Evening walk", duration_minutes=30)
        walk = self._workout_item(external_id="hk-walk", activity="Walk", raw_type="walking", duration_minutes=35)
        resp = self._post({"workouts": [walk]})
        self.assertEqual(resp.data["results"][0]["status"], "matched_planned")
        planned.refresh_from_db()
        self.assertEqual(planned.status, "done")

    def test_short_workout_does_not_complete_planned(self):
        planned = self._planned(duration_minutes=60)
        short = self._workout_item(duration_minutes=20)
        resp = self._post({"workouts": [short]})
        self.assertEqual(resp.data["results"][0]["status"], "created")
        planned.refresh_from_db()
        self.assertEqual(planned.status, "planned")

    def test_window_gate_applies_to_single_candidate(self):
        from datetime import datetime

        # Scheduled 18:00 JST = 09:00Z; HK started 22:30Z (07:30 JST) — outside ±2h
        planned = self._planned(scheduled_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC))
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "created")
        planned.refresh_from_db()
        self.assertEqual(planned.status, "planned")

    def test_window_match_single_candidate(self):
        from datetime import datetime

        # Scheduled 07:00 JST = 2026-06-09 22:00Z; HK start 22:30Z is inside ±2h
        planned = self._planned(scheduled_at=datetime(2026, 6, 9, 22, 0, tzinfo=UTC))
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "matched_planned")
        planned.refresh_from_db()
        self.assertEqual(planned.status, "done")

    def test_multiple_day_only_candidates_is_ambiguous(self):
        self._planned(activity="Run A")
        self._planned(activity="Run B")
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "created")
        self.assertEqual(Workout.objects.filter(tenant=self.tenant, status="planned").count(), 2)

    def test_edit_locked_candidate_creates_standalone(self):
        from django.utils import timezone as dj_tz

        planned = self._planned(edit_lock_until=dj_tz.now() + timedelta(minutes=5), edit_lock_owner="user")
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "created")
        planned.refresh_from_db()
        self.assertEqual(planned.status, "planned")

    def test_matched_flip_enqueues_one_regen(self):
        profile = FuelProfile.objects.get(tenant=self.tenant)
        profile.use_session_scheduling = True
        profile.save(update_fields=["use_session_scheduling"])
        self._planned()
        items = [
            self._workout_item(),
            self._workout_item(external_id="hk-2", started_at="2026-06-08T22:30:00Z"),
            self._workout_item(external_id="hk-3", started_at="2026-06-07T22:30:00Z"),
        ]
        with patch("apps.cron.publish.publish_task") as publish:
            resp = self._post({"workouts": items})
        self.assertEqual(resp.data["summary"]["matched_planned"], 1)
        self.assertEqual(publish.call_count, 1)

    # ── cross-source adopt-guard (don't double-count) ──────────────────

    def _manual_log(self, **overrides):
        """An existing DONE workout the user logged in-app/chat — no
        external_id, so it would escape the HK unique constraint."""
        defaults = dict(
            tenant=self.tenant,
            date=date(2026, 6, 10),
            status="done",
            category="cardio",
            activity="Evening run",
            duration_minutes=40,
            source="user",
        )
        defaults.update(overrides)
        return Workout.objects.create(**defaults)

    def test_hk_adopts_existing_manual_log(self):
        manual = self._manual_log()
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "matched_log")
        self.assertEqual(resp.data["summary"]["matched_log"], 1)
        # No duplicate row — the manual log is adopted, not re-created.
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 1)
        manual.refresh_from_db()
        self.assertEqual(manual.external_id, "hk-uuid-0001")
        self.assertEqual(manual.source, "user")  # authorship preserved
        self.assertEqual(manual.duration_minutes, 40)  # user's value preserved
        self.assertEqual(manual.detail_json["avg_hr"], 152)  # HK metric filled in
        self.assertTrue(manual.detail_json["_healthkit"]["adopted"])
        self.assertIn("Apple Health", manual.notes_thread[-1]["text"])

    def test_adopt_preserves_user_detail_fills_gaps(self):
        # User logged the distance themselves; HK shouldn't clobber it but
        # should add the heart-rate it measured.
        manual = self._manual_log(detail_json={"distance_km": 9.99})
        self._post({"workouts": [self._workout_item()]})
        manual.refresh_from_db()
        self.assertEqual(manual.detail_json["distance_km"], 9.99)  # user value wins
        self.assertEqual(manual.detail_json["avg_hr"], 152)  # HK gap-fill

    def test_adopted_log_resync_is_duplicate(self):
        self._manual_log()
        self._post({"workouts": [self._workout_item()]})
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "duplicate")
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 1)

    def test_hk_does_not_adopt_different_category(self):
        manual = self._manual_log(category="strength", activity="Lifting")
        resp = self._post({"workouts": [self._workout_item()]})  # cardio
        self.assertEqual(resp.data["results"][0]["status"], "created")
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 2)
        manual.refresh_from_db()
        self.assertEqual(manual.external_id, "")

    def test_hk_does_not_adopt_duration_mismatch(self):
        # HK is 42 min; a 10-min manual log is not the same session.
        manual = self._manual_log(duration_minutes=10)
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "created")
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 2)
        manual.refresh_from_db()
        self.assertEqual(manual.external_id, "")

    def test_ambiguous_manual_logs_not_adopted(self):
        # Two same-day same-category day-only logs — no way to know which,
        # so create standalone rather than risk a wrong merge.
        self._manual_log(activity="Run A")
        self._manual_log(activity="Run B")
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "created")
        self.assertEqual(Workout.objects.filter(tenant=self.tenant, external_id="").count(), 2)

    def test_edit_locked_manual_log_not_adopted(self):
        from django.utils import timezone as dj_tz

        manual = self._manual_log(edit_lock_until=dj_tz.now() + timedelta(minutes=5))
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "created")
        manual.refresh_from_db()
        self.assertEqual(manual.external_id, "")

    def test_planned_match_precedes_adopt(self):
        # A planned session AND a manual log both exist for the day — the
        # planned completion wins; the manual log is left untouched.
        planned = self._planned()
        manual = self._manual_log()
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "matched_planned")
        planned.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(planned.status, "done")
        self.assertEqual(planned.external_id, "hk-uuid-0001")
        self.assertEqual(manual.external_id, "")

    # ── daily metrics ──────────────────────────────────────────────────

    def test_daily_metrics_upsert_preserves_quality(self):
        from .models import RestingHeartRateLog, SleepLog

        SleepLog.objects.create(tenant=self.tenant, date=date(2026, 6, 10), duration_hours=Decimal("6.0"), quality=4)
        resp = self._post(
            {"daily_metrics": [{"date": "2026-06-10", "resting_hr": 52, "sleep_hours": 7.4, "body_weight_kg": 72.5}]}
        )
        self.assertEqual(resp.data["daily_results"][0]["status"], "upserted")
        sleep = SleepLog.objects.get(tenant=self.tenant, date=date(2026, 6, 10))
        self.assertEqual(sleep.duration_hours, Decimal("7.4"))
        self.assertEqual(sleep.quality, 4)  # user-entered quality survives
        self.assertEqual(RestingHeartRateLog.objects.get(tenant=self.tenant, date=date(2026, 6, 10)).bpm, 52)
        self.assertEqual(
            BodyWeightLog.objects.get(tenant=self.tenant, date=date(2026, 6, 10)).weight_kg, Decimal("72.5")
        )

    def test_daily_metric_out_of_range(self):
        resp = self._post({"daily_metrics": [{"date": "2026-06-10", "resting_hr": 10}]})
        self.assertEqual(resp.data["daily_results"][0]["status"], "error")

    # ── deletions + tombstones ─────────────────────────────────────────

    def test_deleted_external_ids_removes_and_tombstones(self):
        self._post({"workouts": [self._workout_item()]})
        resp = self._post({"deleted_external_ids": ["hk-uuid-0001"]})
        self.assertEqual(resp.data["summary"]["deleted"], 1)
        self.assertFalse(Workout.objects.filter(tenant=self.tenant, external_id="hk-uuid-0001").exists())
        profile = FuelProfile.objects.get(tenant=self.tenant)
        self.assertIn("hk-uuid-0001", profile.healthkit_tombstones)
        # Anchor-reset re-delivery cannot resurrect it
        resp = self._post({"workouts": [self._workout_item()]})
        self.assertEqual(resp.data["results"][0]["status"], "duplicate")
        self.assertFalse(Workout.objects.filter(tenant=self.tenant, external_id="hk-uuid-0001").exists())

    def test_nbhd_side_delete_tombstones_via_signal(self):
        self._post({"workouts": [self._workout_item()]})
        Workout.objects.get(tenant=self.tenant, external_id="hk-uuid-0001").delete()
        profile = FuelProfile.objects.get(tenant=self.tenant)
        self.assertIn("hk-uuid-0001", profile.healthkit_tombstones)

    def test_deleting_non_healthkit_workout_does_not_tombstone(self):
        w = Workout.objects.create(tenant=self.tenant, date=date(2026, 6, 10), category="cardio", activity="Manual run")
        w.delete()
        profile = FuelProfile.objects.get(tenant=self.tenant)
        self.assertEqual(profile.healthkit_tombstones, [])

    # ── device_tz self-heal ────────────────────────────────────────────

    def test_device_tz_self_heals_utc_default(self):
        self.user.timezone = "UTC"
        self.user.save(update_fields=["timezone"])
        # 16:00Z would be the same UTC day, but 01:00 JST on the 10th
        item = self._workout_item(started_at="2026-06-09T16:00:00Z", ended_at="2026-06-09T16:42:00Z")
        resp = self._post({"device_tz": "Asia/Tokyo", "workouts": [item]})
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "Asia/Tokyo")
        w = Workout.objects.get(tenant=self.tenant, external_id="hk-uuid-0001")
        self.assertEqual(w.date, date(2026, 6, 10))

    def test_device_tz_does_not_override_explicit_timezone(self):
        self.user.timezone = "America/New_York"
        self.user.save(update_fields=["timezone"])
        self._post({"device_tz": "Asia/Tokyo", "workouts": [self._workout_item()]})
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "America/New_York")

    def test_invalid_device_tz_ignored(self):
        self.user.timezone = "UTC"
        self.user.save(update_fields=["timezone"])
        self._post({"device_tz": "Not/AZone", "workouts": [self._workout_item()]})
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "UTC")

    # ── visibility contract ────────────────────────────────────────────

    def test_single_user_md_push_per_batch(self):
        items = [
            self._workout_item(external_id=f"hk-{i}", started_at=f"2026-06-0{(i % 8) + 1}T22:30:00Z") for i in range(10)
        ]
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch("apps.orchestrator.workspace_envelope.push_user_md") as push,
        ):
            resp = self._post({"workouts": items})
        self.assertEqual(resp.data["summary"]["created"], 10)
        self.assertEqual(push.call_count, 1)
        self.assertEqual(push.call_args.kwargs.get("debounce_seconds"), 0)

    def test_no_push_when_nothing_written(self):
        self._post({"workouts": [self._workout_item()]})
        with patch("apps.orchestrator.workspace_envelope.push_user_md") as push:
            self._post({"workouts": [self._workout_item()]})  # pure duplicate
        self.assertEqual(push.call_count, 0)


class HealthKitEnvelopeTests(TestCase):
    """render_fuel additions: tenant-local today, RHR line, metric suffixes."""

    def setUp(self):
        self.tenant = create_tenant(display_name="HK Env Test", telegram_chat_id=800043)
        self.tenant.fuel_enabled = True
        self.tenant.save(update_fields=["fuel_enabled"])

    def test_render_includes_rhr_and_metrics(self):
        from apps.common.tenant_tz import tenant_today

        from .envelope import render_fuel
        from .models import RestingHeartRateLog

        today = tenant_today(self.tenant)
        RestingHeartRateLog.objects.create(tenant=self.tenant, date=today, bpm=52)
        Workout.objects.create(
            tenant=self.tenant,
            date=today,
            status="done",
            category="cardio",
            activity="Run",
            duration_minutes=42,
            detail_json={"distance_km": 5.21, "avg_hr": 152},
        )
        body = render_fuel(self.tenant)
        self.assertIn("Resting HR**: 52 bpm", body)
        self.assertIn("5.21 km", body)
        self.assertIn("152 bpm avg", body)

    def test_stale_rhr_omitted(self):
        from apps.common.tenant_tz import tenant_today

        from .envelope import render_fuel
        from .models import RestingHeartRateLog

        RestingHeartRateLog.objects.create(
            tenant=self.tenant, date=tenant_today(self.tenant) - timedelta(days=10), bpm=52
        )
        body = render_fuel(self.tenant)
        self.assertNotIn("Resting HR", body)


class FuelTrendsDigestTests(TestCase):
    """weekly_trends aggregates + render_fuel trends digest + provenance."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Trends Test", telegram_chat_id=800046)
        self.tenant.fuel_enabled = True
        self.tenant.save(update_fields=["fuel_enabled"])

    def _done(self, days_ago, category, minutes, **overrides):
        from apps.common.tenant_tz import tenant_today

        defaults = dict(
            tenant=self.tenant,
            date=tenant_today(self.tenant) - timedelta(days=days_ago),
            status="done",
            category=category,
            activity=category.title(),
            duration_minutes=minutes,
        )
        defaults.update(overrides)
        return Workout.objects.create(**defaults)

    def test_weekly_trends_empty_when_no_workouts(self):
        from .services import weekly_trends

        self.assertEqual(weekly_trends(self.tenant), {})

    def test_weekly_trends_aggregates(self):
        from .services import weekly_trends

        self._done(0, "strength", 60)
        self._done(2, "cardio", 30)
        self._done(3, "strength", 45)
        self._done(20, "mobility", 20)
        self._done(40, "cardio", 30)  # outside the 28-day window — excluded
        t = weekly_trends(self.tenant)
        self.assertEqual(t["sessions_28d"], 4)
        self.assertEqual(t["minutes_28d"], 155)
        self.assertEqual(t["sessions_7d"], 3)
        self.assertEqual(t["minutes_7d"], 135)
        cats = {c["category"]: c["count"] for c in t["by_category"]}
        self.assertEqual(cats, {"strength": 2, "cardio": 1, "mobility": 1})
        self.assertEqual(t["recency_days"]["strength"], 0)
        self.assertEqual(t["recency_days"]["cardio"], 2)

    def test_render_fuel_includes_trends_digest(self):
        from .envelope import render_fuel

        self._done(0, "strength", 60)
        self._done(2, "cardio", 30)
        body = render_fuel(self.tenant)
        self.assertIn("Trends", body)
        self.assertIn("By activity", body)
        self.assertIn("Last session", body)

    def test_render_fuel_flags_healthkit_provenance(self):
        from .envelope import render_fuel

        self._done(0, "cardio", 42, source="healthkit", detail_json={"distance_km": 5.2})
        body = render_fuel(self.tenant)
        self.assertIn("via Apple Health", body)

    def test_render_fuel_no_provenance_label_for_manual(self):
        from .envelope import render_fuel

        self._done(0, "cardio", 42, source="user")
        body = render_fuel(self.tenant)
        self.assertNotIn("via Apple Health", body)


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class HealthKitRuntimeSummaryTests(TestCase):
    """nbhd_fuel_summary payload carries source, metrics, latest_resting_hr."""

    def setUp(self):
        self.tenant = create_tenant(display_name="HK Summary Test", telegram_chat_id=800044)
        self.tenant.fuel_enabled = True
        self.tenant.save(update_fields=["fuel_enabled"])
        self.client = APIClient()

    def test_summary_includes_health_fields(self):
        from .models import RestingHeartRateLog

        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 6, 10),
            status="done",
            category="cardio",
            activity="Run",
            source="healthkit",
            detail_json={"distance_km": 5.21, "avg_hr": 152, "calories": 380},
        )
        RestingHeartRateLog.objects.create(tenant=self.tenant, date=date(2026, 6, 10), bpm=52)
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/summary/",
            **{
                "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
                "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
            },
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        recent = resp.data["recent_workouts"][0]
        self.assertEqual(recent["source"], "healthkit")
        self.assertEqual(recent["avg_hr"], 152)
        self.assertEqual(recent["distance_km"], 5.21)
        self.assertEqual(resp.data["latest_resting_hr"], {"date": "2026-06-10", "bpm": 52})

    def test_summary_includes_trends(self):
        from apps.common.tenant_tz import tenant_today

        Workout.objects.create(
            tenant=self.tenant,
            date=tenant_today(self.tenant),
            status="done",
            category="cardio",
            activity="Run",
            duration_minutes=30,
        )
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/summary/",
            **{
                "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
                "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
            },
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn("trends", resp.data)
        self.assertEqual(resp.data["trends"]["sessions_28d"], 1)

    def test_runtime_log_tagged_assistant_source(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {"activity": "Run", "category": "cardio", "duration_minutes": 30},
            format="json",
            **{
                "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
                "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
            },
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        w = Workout.objects.get(id=resp.data["id"])
        # Logged via the assistant/chat path → tagged assistant, distinct
        # from a direct in-app log (source=user) or Apple Health (healthkit).
        self.assertEqual(w.source, "assistant")


class HealthKitSyncHardeningTests(TestCase):
    """Regressions from the adversarial review: malformed input must yield
    per-item errors (HTTP 200), never a request-wide 500 — a 5xx wedges the
    iOS anchor-retry loop permanently."""

    def setUp(self):
        self.tenant = create_tenant(display_name="HK Hardening", telegram_chat_id=800045)
        self.tenant.fuel_enabled = True
        self.tenant.save(update_fields=["fuel_enabled"])
        self.user = self.tenant.user
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        FuelProfile.objects.create(tenant=self.tenant)
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def _post(self, payload):
        return self.client.post("/api/v1/fuel/healthkit/sync/", payload, format="json")

    @staticmethod
    def _item(**overrides):
        item = {
            "external_id": "hk-hard-1",
            "activity": "Run",
            "category": "cardio",
            "raw_type": "running",
            "started_at": "2026-06-09T22:30:00Z",
            "duration_minutes": 42,
        }
        item.update(overrides)
        return item

    def test_invalid_calendar_date_is_per_item_error(self):
        # parse_datetime RAISES ValueError on well-formed-but-invalid dates
        bad = self._item(started_at="2026-02-30T10:00:00Z")
        good = self._item(external_id="hk-hard-good")
        resp = self._post({"workouts": [bad, good]})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["results"][0]["status"], "error")
        self.assertEqual(resp.data["results"][1]["status"], "created")

    def test_invalid_ended_at_calendar_date_is_per_item_error(self):
        resp = self._post({"workouts": [self._item(ended_at="2026-02-30T10:00:00Z")]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["results"][0]["status"], "error")

    def test_nonfinite_numerics_are_per_item_errors(self):
        for bad in (
            self._item(duration_minutes="inf"),
            self._item(duration_minutes="nan"),
            self._item(duration_minutes="1e400"),
            self._item(duration_minutes=10**400),
            self._item(metrics={"calories": "1e400"}),
        ):
            resp = self._post({"workouts": [bad]})
            self.assertEqual(resp.status_code, 200, resp.data)
            status_ = resp.data["results"][0]["status"]
            # metric keys are dropped when invalid; only duration is fatal
            self.assertIn(status_, ("error", "created", "duplicate"))
        resp = self._post({"daily_metrics": [{"date": "2026-06-10", "resting_hr": "inf"}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["daily_results"][0]["status"], "error")

    def test_extreme_dates_are_per_item_errors(self):
        for started in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:00:00Z", "2031-01-01T10:00:00Z"):
            resp = self._post({"workouts": [self._item(started_at=started)]})
            self.assertEqual(resp.status_code, 200, resp.data)
            self.assertEqual(resp.data["results"][0]["status"], "error")

    def test_daily_date_out_of_sane_window_is_error(self):
        resp = self._post(
            {
                "daily_metrics": [
                    {"date": "2026-02-30", "resting_hr": 60},
                    {"date": "0008-06-10", "resting_hr": 60},
                    {"date": "2569-06-10", "resting_hr": 60},
                ]
            }
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["status"] for r in resp.data["daily_results"]], ["error", "error", "error"])

    def test_deleted_matched_planned_workout_is_tombstoned(self):
        # Plan a session, sync the matching HK sample, delete the workout,
        # re-sync the same sample after an "anchor reset" — must not return.
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 6, 10),
            status="planned",
            category="cardio",
            activity="Morning run",
            duration_minutes=45,
            source="assistant",
        )
        resp = self._post({"workouts": [self._item()]})
        self.assertEqual(resp.data["results"][0]["status"], "matched_planned")
        workout_id = resp.data["results"][0]["workout_id"]
        resp = self.client.delete(f"/api/v1/fuel/workouts/{workout_id}/")
        self.assertIn(resp.status_code, (200, 204))
        profile = FuelProfile.objects.get(tenant=self.tenant)
        self.assertIn("hk-hard-1", profile.healthkit_tombstones)
        resp = self._post({"workouts": [self._item()]})
        self.assertEqual(resp.data["results"][0]["status"], "duplicate")
        self.assertFalse(Workout.objects.filter(tenant=self.tenant, external_id="hk-hard-1").exists())
