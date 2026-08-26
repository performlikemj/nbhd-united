"""Regression tests for merge-by-default runtime plan schedule updates."""

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .models import PlanSlot, Workout, WorkoutPlan

_PRESCRIPTION = {
    "exercises": [
        {
            "name": "Bench Press",
            "sets": [{"type": "weighted_reps", "reps": 5, "weight": 60}],
        }
    ]
}


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeUpdatePlanMergeTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Plan Merge", telegram_chat_id=800601)
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        self.plan_start = date.today() + timedelta(days=((7 - date.today().weekday()) % 7) or 7)
        cron_patch = patch("apps.fuel.runtime_views._manage_fuel_cron", return_value=None)
        cron_patch.start()
        self.addCleanup(cron_patch.stop)

    def _plan_url(self, plan):
        return f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{plan.id}/"

    def _create_weekday_plan(self):
        schedule = {
            day: {
                "category": "strength",
                "activity": f"Day {day}",
                "detail_json": _PRESCRIPTION,
            }
            for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
        }
        response = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/",
            {
                "name": "Five Day Plan",
                "start_date": self.plan_start.isoformat(),
                "weeks": 1,
                "days_per_week": 5,
                "schedule_json": schedule,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.data)
        return WorkoutPlan.objects.get(id=response.data["id"])

    def test_partial_weekend_schedule_merges_without_deleting_weekdays(self):
        plan = self._create_weekday_plan()
        weekday_slot_ids = set(
            PlanSlot.objects.filter(plan=plan, weekday__lte=4, archived_at__isnull=True).values_list("id", flat=True)
        )
        weekday_workout_ids = set(
            Workout.objects.filter(plan=plan, date__week_day__in=(2, 3, 4, 5, 6)).values_list("id", flat=True)
        )

        response = self.client.patch(
            self._plan_url(plan),
            {
                "schedule_json": {
                    "saturday": {"category": "mobility", "activity": "Mobility"},
                    "sunday": {"category": "mobility", "activity": "Recovery Flow"},
                }
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        plan.refresh_from_db()
        self.assertEqual(set(plan.schedule_json), {str(day) for day in range(7)})
        self.assertEqual(
            set(
                PlanSlot.objects.filter(plan=plan, weekday__lte=4, archived_at__isnull=True).values_list(
                    "id", flat=True
                )
            ),
            weekday_slot_ids,
        )
        self.assertFalse(PlanSlot.objects.filter(plan=plan, weekday__lte=4, archived_at__isnull=False).exists())
        self.assertTrue(
            weekday_workout_ids.issubset(set(Workout.objects.filter(plan=plan).values_list("id", flat=True)))
        )
        self.assertEqual(Workout.objects.filter(plan=plan).count(), 7)
        self.assertEqual(
            set(Workout.objects.filter(plan=plan, date__week_day__in=(1, 7)).values_list("activity", flat=True)),
            {"Mobility", "Recovery Flow"},
        )

    def test_partial_day_omitting_detail_keeps_existing_prescription(self):
        plan = self._create_weekday_plan()

        response = self.client.patch(
            self._plan_url(plan),
            {
                "schedule_json": {
                    "monday": {
                        "category": "strength",
                        "activity": "Updated Monday",
                        "duration_minutes": 70,
                    }
                }
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        plan.refresh_from_db()
        self.assertEqual(plan.schedule_json["0"]["detail_json"], _PRESCRIPTION)
        monday = Workout.objects.get(plan=plan, date=self.plan_start)
        self.assertEqual(monday.detail_json, _PRESCRIPTION)
        self.assertEqual(monday.activity, "Updated Monday")

    def test_remove_days_archives_and_deletes_only_named_day(self):
        plan = self._create_weekday_plan()
        kept_ids = set(Workout.objects.filter(plan=plan).exclude(date__week_day=6).values_list("id", flat=True))

        response = self.client.patch(
            self._plan_url(plan),
            {"remove_days": ["friday"]},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        plan.refresh_from_db()
        self.assertEqual(set(plan.schedule_json), {"0", "1", "2", "3"})
        self.assertEqual(
            list(PlanSlot.objects.filter(plan=plan, archived_at__isnull=False).values_list("weekday", flat=True)),
            [4],
        )
        self.assertFalse(Workout.objects.filter(plan=plan, date__week_day=6).exists())
        self.assertEqual(set(Workout.objects.filter(plan=plan).values_list("id", flat=True)), kept_ids)

    def test_replace_schedule_true_replaces_whole_template(self):
        plan = self._create_weekday_plan()

        response = self.client.patch(
            self._plan_url(plan),
            {
                "schedule_json": {
                    "saturday": {"category": "mobility", "activity": "Mobility"},
                    "sunday": {"category": "mobility", "activity": "Recovery Flow"},
                },
                "replace_schedule": True,
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        plan.refresh_from_db()
        self.assertEqual(set(plan.schedule_json), {"5", "6"})
        self.assertEqual(
            set(PlanSlot.objects.filter(plan=plan, archived_at__isnull=False).values_list("weekday", flat=True)),
            {0, 1, 2, 3, 4},
        )
        self.assertEqual(Workout.objects.filter(plan=plan).count(), 2)
        self.assertEqual(
            set(Workout.objects.filter(plan=plan).values_list("activity", flat=True)),
            {"Mobility", "Recovery Flow"},
        )

    def test_implicit_null_removal_of_prescribed_day_self_corrects(self):
        plan = self._create_weekday_plan()
        original_schedule = plan.schedule_json

        response = self.client.patch(
            self._plan_url(plan),
            {"schedule_json": {"friday": None}},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"], "validation_failed")
        self.assertIn(
            "schedule_json merges; to drop days pass remove_days or replace_schedule", response.data["message"]
        )
        self.assertEqual(response.data["details"][0]["type"], "explicit_removal_required")
        plan.refresh_from_db()
        self.assertEqual(plan.schedule_json, original_schedule)
        self.assertTrue(Workout.objects.filter(plan=plan, date__week_day=6).exists())

    def test_duplicate_numeric_and_name_keys_still_rejected(self):
        plan = self._create_weekday_plan()

        response = self.client.patch(
            self._plan_url(plan),
            {
                "schedule_json": {
                    "4": {"category": "mobility", "activity": "Mobility A"},
                    "friday": {"category": "mobility", "activity": "Mobility B"},
                }
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"], "invalid_schedule")
        self.assertIn("both mean friday", response.data["detail"])
