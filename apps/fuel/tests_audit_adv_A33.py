"""Adversarial-audit regression tests for cluster A33.

fuel#2: WorkoutPlanListView.post (and WorkoutPlanDetailView.patch) were missing
three guards that the runtime twin (RuntimeWorkoutPlanListCreateView) already
had:

  (a) NO transaction.atomic() — a mid-loop failure in _expand_plan_workouts
      left an orphaned plan row plus a partial calendar committed with no way
      to roll them back.

  (b) NO schedule_json validation — non-dict day values passed DRF validation
      unchecked and raised AttributeError inside _expand_plan_workouts, after
      the plan row was already committed.

  (c) NO idempotency dedup — a double-submit created a duplicate plan and its
      whole calendar rather than short-circuiting with 200 deduped.

Fix: views.py WorkoutPlanListView.post + WorkoutPlanDetailView.patch now
validate schedule_json via _validate_normalize_schedule before persisting, wrap
create/regen in transaction.atomic(), and deduplicate on name+start_date+ACTIVE.
"""

from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.fuel.models import Workout, WorkoutPlan
from apps.tenants.services import create_tenant


def _make_valid_schedule():
    return {
        "0": {"category": "strength", "activity": "Squat", "target_rpe": 8},
        "2": {"category": "cardio", "activity": "Run", "duration_minutes": 30},
    }


@override_settings(
    SIMPLE_JWT={"SIGNING_KEY": "test-secret-key-a33"},
    NBHD_INTERNAL_API_KEY="test-internal-key-a33",
)
class WorkoutPlanListViewPostTests(TestCase):
    """WorkoutPlanListView.post parity guards (fuel#2)."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Plan A33", telegram_chat_id=833001)
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.tenant.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
        self.url = "/api/v1/fuel/plans/"

    # ------------------------------------------------------------------
    # (a) Atomicity — mid-loop failure rolls back the plan row
    # ------------------------------------------------------------------

    def test_post_rolls_back_plan_on_expand_failure(self):
        """If _expand_plan_workouts raises mid-loop, the plan row must NOT be
        committed — the whole unit must be atomic (fuel#2 guard a)."""
        plan_count_before = WorkoutPlan.objects.filter(tenant=self.tenant).count()

        with patch(
            "apps.fuel.runtime_views._expand_plan_workouts",
            side_effect=RuntimeError("simulated mid-loop failure"),
        ):
            resp = self.client.post(
                self.url,
                data={
                    "name": "Rollback Test Plan",
                    "weeks": 2,
                    "days_per_week": 2,
                    "start_date": "2026-07-07",
                    "schedule_json": _make_valid_schedule(),
                },
                format="json",
            )

        # Must return 400, not 201
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("error", resp.data)

        # No orphaned plan row committed
        plan_count_after = WorkoutPlan.objects.filter(tenant=self.tenant).count()
        self.assertEqual(plan_count_before, plan_count_after, "plan row must be rolled back on expand failure")

    # ------------------------------------------------------------------
    # (b) Schedule validation — non-dict day value rejected with 400
    # ------------------------------------------------------------------

    def test_post_rejects_non_dict_schedule_day_with_400(self):
        """A schedule_json where a day value is not a dict must return 400
        before any DB write, not raise AttributeError after committing the plan
        (fuel#2 guard b)."""
        bad_schedule = {"0": "just a string, not a dict"}
        plan_count_before = WorkoutPlan.objects.filter(tenant=self.tenant).count()

        resp = self.client.post(
            self.url,
            data={
                "name": "Bad Schedule Plan",
                "weeks": 1,
                "days_per_week": 1,
                "start_date": "2026-07-07",
                "schedule_json": bad_schedule,
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        # No plan row must have been created
        plan_count_after = WorkoutPlan.objects.filter(tenant=self.tenant).count()
        self.assertEqual(plan_count_before, plan_count_after, "plan row must not be created on invalid schedule")

    # ------------------------------------------------------------------
    # (c) Idempotency — double-submit returns 200 deduped, not a duplicate plan
    # ------------------------------------------------------------------

    def test_post_deduplicates_same_name_start_date(self):
        """A second POST with the same name + start_date for an ACTIVE plan must
        return 200 with deduped=True rather than creating a duplicate plan row
        (fuel#2 guard c)."""
        payload = {
            "name": "Dedup Plan",
            "weeks": 2,
            "days_per_week": 2,
            "start_date": "2026-07-07",
            "schedule_json": _make_valid_schedule(),
        }

        # First create — must succeed with 201
        resp1 = self.client.post(self.url, data=payload, format="json")
        self.assertEqual(resp1.status_code, 201, resp1.data)

        # Second identical create — must be deduped with 200
        resp2 = self.client.post(self.url, data=payload, format="json")
        self.assertEqual(resp2.status_code, 200, resp2.data)
        self.assertTrue(resp2.data.get("deduped"), "second POST must carry deduped=True")

        # Only one plan row must exist
        count = WorkoutPlan.objects.filter(tenant=self.tenant, name="Dedup Plan").count()
        self.assertEqual(count, 1, "double-submit must not create a duplicate plan row")


@override_settings(
    SIMPLE_JWT={"SIGNING_KEY": "test-secret-key-a33"},
    NBHD_INTERNAL_API_KEY="test-internal-key-a33",
)
class WorkoutPlanDetailViewPatchTests(TestCase):
    """WorkoutPlanDetailView.patch parity guards (fuel#2)."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Plan A33 Patch", telegram_chat_id=833002)
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.tenant.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        # Seed a plan we can PATCH against
        self.plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Existing Plan",
            weeks=2,
            days_per_week=2,
            start_date=date(2026, 7, 7),
            schedule_json=_make_valid_schedule(),
        )

    def _url(self):
        return f"/api/v1/fuel/plans/{self.plan.id}/"

    # ------------------------------------------------------------------
    # (b) Schedule validation on PATCH
    # ------------------------------------------------------------------

    def test_patch_rejects_non_dict_schedule_day_with_400(self):
        """PATCH with a non-dict day value in schedule_json must return 400 and
        leave the plan and its calendar untouched (fuel#2 guard b on patch path)."""
        workout_count_before = Workout.objects.filter(plan=self.plan).count()
        original_schedule = dict(self.plan.schedule_json)

        resp = self.client.patch(
            self._url(),
            data={"schedule_json": {"0": "bad string not a dict"}},
            format="json",
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        # Plan schedule must be unchanged
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.schedule_json, original_schedule)
        # Calendar must be unchanged
        self.assertEqual(Workout.objects.filter(plan=self.plan).count(), workout_count_before)

    # ------------------------------------------------------------------
    # (a) Atomicity on PATCH regen
    # ------------------------------------------------------------------

    @patch("apps.fuel.views.today_in_tenant_tz", return_value=date(2026, 7, 7))
    def test_patch_rolls_back_on_expand_failure(self, _mock_today):
        """If _expand_plan_workouts raises during PATCH regen, the plan update
        must be rolled back so the plan remains in its pre-patch state
        (fuel#2 guard a on patch path)."""
        original_weeks = self.plan.weeks

        with patch(
            "apps.fuel.runtime_views._expand_plan_workouts",
            side_effect=RuntimeError("simulated regen failure"),
        ):
            resp = self.client.patch(
                self._url(),
                data={"weeks": 3},
                format="json",
            )

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("error", resp.data)

        # Plan must not have been updated
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.weeks, original_weeks, "plan weeks must be rolled back on regen failure")
