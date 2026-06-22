"""Regression tests for fix cluster C05 (fuel backend).

Covers:
- FA-0567: RestingHRDetailView.patch returns 409 (not 500) on a date collision.
- FA-0568: SleepDetailView.patch returns 409 (not 500) on a date collision.
- FA-0555: WorkoutDuplicateView dates the copy in the tenant's timezone.
- FA-0584: RuntimeWorkoutPlanDetailView.patch validates schedule_json sets
  (returns the self-correcting 400 envelope) instead of persisting bad data.
- FA-0570/FA-0582: covered by behavioural assertions on the runtime PATCH /
  POST paths below.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.tenants.services import create_tenant

from .models import RestingHeartRateLog, SleepLog, Workout, WorkoutPlan


class SleepRestingHRConflictTests(TestCase):
    """FA-0567 / FA-0568: PATCH onto an occupied date is a clean 409."""

    def setUp(self):
        self.tenant = create_tenant(display_name="C05 Conflict", telegram_chat_id=800501)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_resting_hr_patch_date_collision_returns_409(self):
        RestingHeartRateLog.objects.create(tenant=self.tenant, date=date(2026, 6, 1), bpm=55)
        movable = RestingHeartRateLog.objects.create(tenant=self.tenant, date=date(2026, 6, 2), bpm=58)
        resp = self.client.patch(
            f"/api/v1/fuel/resting-hr/{movable.id}/",
            {"date": "2026-06-01"},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"], "date_conflict")
        # The mover is untouched — the DB constraint never fired.
        movable.refresh_from_db()
        self.assertEqual(movable.date, date(2026, 6, 2))

    def test_resting_hr_patch_same_date_still_ok(self):
        entry = RestingHeartRateLog.objects.create(tenant=self.tenant, date=date(2026, 6, 1), bpm=55)
        resp = self.client.patch(
            f"/api/v1/fuel/resting-hr/{entry.id}/",
            {"bpm": 60},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.bpm, 60)

    def test_sleep_patch_date_collision_returns_409(self):
        SleepLog.objects.create(tenant=self.tenant, date=date(2026, 6, 1), duration_hours="7.5")
        movable = SleepLog.objects.create(tenant=self.tenant, date=date(2026, 6, 2), duration_hours="8.0")
        resp = self.client.patch(
            f"/api/v1/fuel/sleep/{movable.id}/",
            {"date": "2026-06-01"},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"], "date_conflict")
        movable.refresh_from_db()
        self.assertEqual(movable.date, date(2026, 6, 2))

    def test_sleep_patch_move_to_free_date_ok(self):
        entry = SleepLog.objects.create(tenant=self.tenant, date=date(2026, 6, 2), duration_hours="8.0")
        resp = self.client.patch(
            f"/api/v1/fuel/sleep/{entry.id}/",
            {"date": "2026-06-05"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.date, date(2026, 6, 5))


class WorkoutDuplicateTimezoneTests(TestCase):
    """FA-0555: the duplicate's date honors the tenant's local day."""

    def setUp(self):
        self.tenant = create_tenant(display_name="C05 Dup", telegram_chat_id=800502)
        self.user = self.tenant.user
        self.user.timezone = "Asia/Tokyo"  # UTC+9
        self.user.save(update_fields=["timezone"])
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_duplicate_uses_tenant_local_date(self):
        source = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 6, 1),
            category="strength",
            activity="Push",
            duration_minutes=60,
        )
        # 2026-06-02 01:00 UTC = 2026-06-02 10:00 JST. Both still on Jun 2,
        # but UTC at 2026-06-01 16:00 (=Jun 2 01:00 JST) would be Jun 1.
        # Freeze just after JST midnight while UTC is still the prior day.
        frozen = datetime(2026, 6, 1, 16, 0, 0, tzinfo=UTC)  # = 2026-06-02 01:00 JST
        with patch("apps.common.llm_contracts.dj_tz.now", return_value=frozen):
            resp = self.client.post(f"/api/v1/fuel/workouts/{source.id}/duplicate/")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["date"], "2026-06-02")
        self.assertEqual(resp.data["status"], "planned")


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimePlanPatchValidationTests(TestCase):
    """FA-0584: PATCH validates schedule_json sets like POST does."""

    def setUp(self):
        self.tenant = create_tenant(display_name="C05 RT Plan", telegram_chat_id=800503)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        self.plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Strength Block",
            start_date=date(2026, 4, 27),
            weeks=2,
            days_per_week=1,
            schedule_json={"0": {"activity": "Push", "category": "strength"}},
        )

    def test_patch_rejects_malformed_strength_set(self):
        # A negative-rep weighted set fails the typed contract; POST rejects
        # it with a 400 envelope and PATCH must do the same (not silently
        # persist it into future planned workouts).
        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{self.plan.id}/",
            {
                "schedule_json": {
                    "0": {
                        "activity": "Push",
                        "category": "strength",
                        "detail_json": {"exercises": [{"name": "Bench", "sets": [{"reps": -3, "weight": 80}]}]},
                    }
                }
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 400)
        # The plan's schedule was NOT mutated by the rejected PATCH.
        self.plan.refresh_from_db()
        self.assertNotIn("detail_json", self.plan.schedule_json["0"])

    def test_patch_accepts_valid_strength_set(self):
        resp = self.client.patch(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{self.plan.id}/",
            {
                "schedule_json": {
                    "0": {
                        "activity": "Push",
                        "category": "strength",
                        "detail_json": {"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 80}]}]},
                    }
                }
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.schedule_json["0"]["detail_json"]["exercises"][0]["name"], "Bench")
