"""Preview→confirm handshakes for destructive Fuel runtime tools."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .models import BodyWeightLog, Workout, WorkoutPlan, WorkoutStatus


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class FuelDeleteConfirmHandshakeTests(TestCase):
    guidance = (
        "Show this exact deletion preview to the user and wait for an explicit yes; then call again with "
        "confirm_token unchanged."
    )

    def setUp(self):
        self.tenant = create_tenant(display_name="Fuel Delete Confirm", telegram_chat_id=702001)
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _delete_workout(self, workout, token=None):
        body = {"confirm_token": token} if token else {}
        return self.client.delete(
            f"/api/v1/fuel/runtime/{self.tenant.id}/workouts/{workout.id}/",
            body,
            format="json",
            **self.headers,
        )

    def _delete_weight(self, weight_date, token=None):
        body = {"confirm_token": token} if token else {}
        return self.client.delete(
            f"/api/v1/fuel/runtime/{self.tenant.id}/body-weight/?date={weight_date}",
            body,
            format="json",
            **self.headers,
        )

    def _delete_plan(self, plan, token=None):
        body = {"confirm_token": token} if token else {}
        return self.client.delete(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{plan.id}/",
            body,
            format="json",
            **self.headers,
        )

    def _workout(self, *, activity="Push", workout_date=date(2026, 8, 28)):
        return Workout.objects.create(
            tenant=self.tenant,
            date=workout_date,
            status=WorkoutStatus.DONE,
            category="strength",
            activity=activity,
            detail_json={
                "exercises": [{"name": "Bench Press", "sets": [{"type": "weighted_reps", "reps": 5, "weight": 80}]}]
            },
        )

    def _plan(self, *, name="Strength Plan"):
        return WorkoutPlan.objects.create(
            tenant=self.tenant,
            name=name,
            start_date=date(2026, 8, 24),
            weeks=4,
            days_per_week=2,
            schedule_json={},
        )

    def test_workout_preview_is_inert_and_matching_token_deletes(self):
        workout = self._workout()

        preview_response = self._delete_workout(workout)

        self.assertEqual(preview_response.status_code, 200)
        preview = preview_response.data
        self.assertEqual(preview["preview"]["date"], "2026-08-28")
        self.assertEqual(preview["preview"]["exercises"][0]["name"], "Bench Press")
        self.assertTrue(Workout.objects.filter(id=workout.id).exists())

        confirmed = self._delete_workout(workout, preview["confirm_token"])

        self.assertEqual(confirmed.status_code, 200)
        self.assertFalse(Workout.objects.filter(id=workout.id).exists())

    def test_workout_wrong_expired_and_parameter_changed_tokens_are_inert(self):
        workout = self._workout()
        other = self._workout(activity="Pull", workout_date=date(2026, 8, 29))
        preview = self._delete_workout(workout).data

        wrong = self._delete_workout(workout, "wrong")
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.data["guidance"], self.guidance)

        changed = self._delete_workout(other, preview["confirm_token"])
        self.assertEqual(changed.status_code, 400)
        self.assertEqual(changed.data["reason"], "mismatch")
        self.assertEqual(changed.data["guidance"], self.guidance)

        issued_at = 1_800_000_000
        with patch("django.core.signing.time.time", return_value=issued_at):
            expiring = self._delete_workout(workout).data["confirm_token"]
        with patch("django.core.signing.time.time", return_value=issued_at + 601):
            expired = self._delete_workout(workout, expiring)
        self.assertEqual(expired.status_code, 400)
        self.assertEqual(expired.data["reason"], "expired")
        self.assertEqual(expired.data["guidance"], self.guidance)
        self.assertEqual(Workout.objects.filter(id__in=[workout.id, other.id]).count(), 2)

    def test_body_weight_preview_is_inert_and_matching_token_deletes(self):
        entry = BodyWeightLog.objects.create(
            tenant=self.tenant,
            date=date(2026, 8, 28),
            weight_kg=Decimal("82.50"),
        )

        preview_response = self._delete_weight("2026-08-28")

        self.assertEqual(preview_response.status_code, 200)
        preview = preview_response.data
        self.assertEqual(preview["preview"]["date"], "2026-08-28")
        self.assertEqual(preview["preview"]["weight_kg"], "82.50")
        self.assertTrue(BodyWeightLog.objects.filter(id=entry.id).exists())

        confirmed = self._delete_weight("2026-08-28", preview["confirm_token"])

        self.assertEqual(confirmed.status_code, 200)
        self.assertFalse(BodyWeightLog.objects.filter(id=entry.id).exists())

    def test_body_weight_wrong_expired_and_parameter_changed_tokens_are_inert(self):
        first = BodyWeightLog.objects.create(
            tenant=self.tenant,
            date=date(2026, 8, 28),
            weight_kg=Decimal("82.50"),
        )
        second = BodyWeightLog.objects.create(
            tenant=self.tenant,
            date=date(2026, 8, 29),
            weight_kg=Decimal("82.00"),
        )
        preview = self._delete_weight("2026-08-28").data

        wrong = self._delete_weight("2026-08-28", "wrong")
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.data["guidance"], self.guidance)

        changed = self._delete_weight("2026-08-29", preview["confirm_token"])
        self.assertEqual(changed.status_code, 400)
        self.assertEqual(changed.data["reason"], "mismatch")
        self.assertEqual(changed.data["guidance"], self.guidance)

        issued_at = 1_800_000_000
        with patch("django.core.signing.time.time", return_value=issued_at):
            expiring = self._delete_weight("2026-08-28").data["confirm_token"]
        with patch("django.core.signing.time.time", return_value=issued_at + 601):
            expired = self._delete_weight("2026-08-28", expiring)
        self.assertEqual(expired.status_code, 400)
        self.assertEqual(expired.data["reason"], "expired")
        self.assertEqual(expired.data["guidance"], self.guidance)
        self.assertEqual(BodyWeightLog.objects.filter(id__in=[first.id, second.id]).count(), 2)

    @patch("apps.fuel.runtime_views._manage_fuel_cron")
    def test_plan_preview_is_inert_and_matching_token_deletes(self, _manage_cron):
        plan = self._plan()
        planned = Workout.objects.create(
            tenant=self.tenant,
            plan=plan,
            date=date(2026, 8, 31),
            status=WorkoutStatus.PLANNED,
            category="strength",
            activity="Push",
        )
        completed = self._workout(activity="Completed")
        completed.plan = plan
        completed.save(update_fields=["plan"])

        preview_response = self._delete_plan(plan)

        self.assertEqual(preview_response.status_code, 200)
        preview = preview_response.data
        self.assertEqual(preview["preview"]["name"], "Strength Plan")
        self.assertEqual(preview["preview"]["future_workout_count"], 1)
        self.assertEqual(preview["preview"]["completed_workout_unlink_count"], 1)
        self.assertTrue(WorkoutPlan.objects.filter(id=plan.id).exists())

        confirmed = self._delete_plan(plan, preview["confirm_token"])

        self.assertEqual(confirmed.status_code, 204)
        self.assertFalse(WorkoutPlan.objects.filter(id=plan.id).exists())
        self.assertFalse(Workout.objects.filter(id=planned.id).exists())
        completed.refresh_from_db()
        self.assertIsNone(completed.plan_id)

    def test_plan_wrong_expired_and_parameter_changed_tokens_are_inert(self):
        plan = self._plan()
        other = self._plan(name="Other Plan")
        preview = self._delete_plan(plan).data

        wrong = self._delete_plan(plan, "wrong")
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.data["guidance"], self.guidance)

        changed = self._delete_plan(other, preview["confirm_token"])
        self.assertEqual(changed.status_code, 400)
        self.assertEqual(changed.data["reason"], "mismatch")
        self.assertEqual(changed.data["guidance"], self.guidance)

        issued_at = 1_800_000_000
        with patch("django.core.signing.time.time", return_value=issued_at):
            expiring = self._delete_plan(plan).data["confirm_token"]
        with patch("django.core.signing.time.time", return_value=issued_at + 601):
            expired = self._delete_plan(plan, expiring)
        self.assertEqual(expired.status_code, 400)
        self.assertEqual(expired.data["reason"], "expired")
        self.assertEqual(expired.data["guidance"], self.guidance)
        self.assertEqual(WorkoutPlan.objects.filter(id__in=[plan.id, other.id]).count(), 2)
