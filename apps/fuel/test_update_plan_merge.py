"""Regression tests for merge-by-default runtime plan schedule updates."""

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .catalog_annotation import annotate_incoming, incoming_name_paths
from .models import PlanSlot, Workout, WorkoutPlan
from .runtime_views import _normalize_stored_schedule_keys

_PRESCRIPTION = {
    "exercises": [
        {
            "name": "Bench Press",
            "sets": [{"type": "weighted_reps", "reps": 5, "weight": 60}],
        }
    ]
}
_MOBILITY_PRESCRIPTION = {
    "skills": [
        {"name": "Hip flexor stretch", "sets": [{"type": "hold_time", "hold_s": 45}]},
    ]
}


def _cataloged(detail):
    return annotate_incoming(detail, incoming_name_paths(detail))[0]


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

    def _create_plan(self, schedule, *, name="Test Plan"):
        response = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/plans/",
            {
                "name": name,
                "start_date": self.plan_start.isoformat(),
                "weeks": 1,
                "days_per_week": len(schedule),
                "schedule_json": schedule,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.data)
        return WorkoutPlan.objects.get(id=response.data["id"])

    def _create_weekday_plan(self):
        schedule = {
            day: {
                "category": "strength",
                "activity": f"Day {day}",
                "detail_json": _PRESCRIPTION,
            }
            for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
        }
        return self._create_plan(schedule, name="Five Day Plan")

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
                    "saturday": {
                        "category": "mobility",
                        "activity": "Mobility",
                        "detail_json": _MOBILITY_PRESCRIPTION,
                    },
                    "sunday": {
                        "category": "mobility",
                        "activity": "Recovery Flow",
                        "detail_json": _MOBILITY_PRESCRIPTION,
                    },
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

    def test_replace_schedule_false_explicitly_merges(self):
        plan = self._create_weekday_plan()

        response = self.client.patch(
            self._plan_url(plan),
            {
                "schedule_json": {
                    "saturday": {
                        "category": "mobility",
                        "activity": "Mobility",
                        "detail_json": _MOBILITY_PRESCRIPTION,
                    }
                },
                "replace_schedule": False,
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        plan.refresh_from_db()
        self.assertEqual(set(plan.schedule_json), {"0", "1", "2", "3", "4", "5"})
        self.assertTrue(Workout.objects.filter(plan=plan, date__week_day=6).exists())

    def test_rename_only_inherits_all_stored_day_fields(self):
        plan = self._create_plan(
            {
                "monday": {
                    "category": "strength",
                    "activity": "Heavy Day",
                    "duration_minutes": 65,
                    "target_rpe": 8,
                    "detail_json": _PRESCRIPTION,
                }
            }
        )

        response = self.client.patch(
            self._plan_url(plan),
            {"schedule_json": {"monday": {"activity": "Renamed Heavy Day"}}},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        plan.refresh_from_db()
        self.assertEqual(
            plan.schedule_json["0"],
            {
                "category": "strength",
                "activity": "Renamed Heavy Day",
                "duration_minutes": 65,
                "target_rpe": 8,
                "detail_json": _cataloged(_PRESCRIPTION),
            },
        )
        monday = Workout.objects.get(plan=plan, date=self.plan_start)
        self.assertEqual(monday.category, "strength")
        self.assertEqual(monday.detail_json, _cataloged(_PRESCRIPTION))
        self.assertEqual(monday.activity, "Renamed Heavy Day")
        self.assertEqual(monday.duration_minutes, 65)
        self.assertEqual(monday.rpe, 8)

    def test_category_omitted_from_new_day_uses_normal_default(self):
        plan = self._create_weekday_plan()

        response = self.client.patch(
            self._plan_url(plan),
            {"schedule_json": {"saturday": {"activity": "Easy Day"}}},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        plan.refresh_from_db()
        self.assertEqual(plan.schedule_json["5"]["category"], "other")
        self.assertEqual(plan.schedule_json["5"]["activity"], "Easy Day")

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

    def test_remove_days_validation_errors_self_correct(self):
        plan = self._create_weekday_plan()
        cases = (
            ("non-list", "friday", "list_type"),
            ("unknown day", ["funday"], "invalid_weekday"),
            # Legacy integer weekdays are zero-based, so Friday is 4.
            ("duplicate normalized day", ["fri", 4], "duplicate_weekday"),
        )

        for label, remove_days, error_type in cases:
            with self.subTest(label=label):
                response = self.client.patch(
                    self._plan_url(plan),
                    {"remove_days": remove_days},
                    format="json",
                    **self.headers,
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["error"], "validation_failed")
                self.assertEqual(response.data["details"][0]["type"], error_type)

    def test_schedule_json_and_remove_days_collision_is_rejected(self):
        plan = self._create_weekday_plan()
        original_schedule = plan.schedule_json

        response = self.client.patch(
            self._plan_url(plan),
            {
                "schedule_json": {"friday": {"category": "mobility", "activity": "Recovery"}},
                "remove_days": ["fri"],
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"], "validation_failed")
        self.assertIn("cannot target the same day", response.data["message"])
        self.assertEqual(response.data["details"][0]["type"], "schedule_remove_conflict")
        plan.refresh_from_db()
        self.assertEqual(plan.schedule_json, original_schedule)

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

    def test_implicit_null_removal_of_unprescribed_day_self_corrects(self):
        plan = self._create_weekday_plan()
        Workout.objects.filter(plan=plan, date__week_day=6).update(detail_json={})

        response = self.client.patch(
            self._plan_url(plan),
            {"schedule_json": {"friday": None}},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"], "validation_failed")
        self.assertIn("remove_days or replace_schedule", response.data["message"])
        self.assertEqual(response.data["details"][0]["type"], "explicit_removal_required")

    def test_replace_schedule_null_day_says_drop_key_or_use_remove_days(self):
        plan = self._create_weekday_plan()

        response = self.client.patch(
            self._plan_url(plan),
            {"schedule_json": {"friday": None}, "replace_schedule": True},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("replace_schedule is already true", response.data["message"])
        self.assertIn("omit their keys from schedule_json or use remove_days", response.data["message"])
        self.assertEqual(response.data["details"][0]["msg"], "drop the key from schedule_json or use remove_days")

    def test_category_flip_omitting_detail_requires_prescription(self):
        plan = self._create_plan(
            {
                "monday": {
                    "category": "mobility",
                    "activity": "Mobility",
                    "detail_json": _MOBILITY_PRESCRIPTION,
                }
            }
        )
        original_schedule = plan.schedule_json

        response = self.client.patch(
            self._plan_url(plan),
            {"schedule_json": {"monday": {"category": "strength", "activity": "Lift"}}},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"], "validation_failed")
        self.assertEqual(response.data["details"][0]["type"], "missing_prescription")
        plan.refresh_from_db()
        self.assertEqual(plan.schedule_json, original_schedule)

    def test_strength_to_mobility_flip_without_detail_requires_prescription(self):
        plan = self._create_plan({"monday": {"category": "strength", "activity": "Lift", "detail_json": _PRESCRIPTION}})
        original_schedule = plan.schedule_json

        response = self.client.patch(
            self._plan_url(plan),
            {"schedule_json": {"monday": {"category": "mobility", "activity": "Mobility"}}},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        plan.refresh_from_db()
        self.assertEqual(plan.schedule_json, original_schedule)
        monday = Workout.objects.get(plan=plan, date=self.plan_start)
        self.assertEqual(monday.category, "strength")
        self.assertEqual(monday.detail_json, _cataloged(_PRESCRIPTION))

    def test_category_flip_with_detail_is_accepted(self):
        plan = self._create_plan(
            {
                "monday": {
                    "category": "mobility",
                    "activity": "Mobility",
                    "detail_json": _MOBILITY_PRESCRIPTION,
                }
            }
        )

        response = self.client.patch(
            self._plan_url(plan),
            {
                "schedule_json": {
                    "monday": {
                        "category": "strength",
                        "activity": "Lift",
                        "detail_json": _PRESCRIPTION,
                    }
                }
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        plan.refresh_from_db()
        self.assertEqual(plan.schedule_json["0"]["category"], "strength")
        self.assertEqual(plan.schedule_json["0"]["detail_json"], _cataloged(_PRESCRIPTION))
        monday = Workout.objects.get(plan=plan, date=self.plan_start)
        self.assertEqual(monday.detail_json, _cataloged(_PRESCRIPTION))

    def test_merge_normalizes_legacy_name_keyed_stored_schedule(self):
        plan = self._create_weekday_plan()
        weekday_names = ("monday", "tuesday", "wednesday", "thursday", "friday")
        legacy_schedule = {weekday_names[int(day)]: day_def for day, day_def in plan.schedule_json.items()}
        WorkoutPlan.objects.filter(id=plan.id).update(schedule_json=legacy_schedule)
        original_workout_ids = set(Workout.objects.filter(plan=plan).values_list("id", flat=True))

        response = self.client.patch(
            self._plan_url(plan),
            {
                "schedule_json": {
                    "saturday": {
                        "category": "mobility",
                        "activity": "Mobility",
                        "detail_json": _MOBILITY_PRESCRIPTION,
                    }
                }
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        plan.refresh_from_db()
        self.assertEqual(set(plan.schedule_json), {"0", "1", "2", "3", "4", "5"})
        self.assertTrue(
            original_workout_ids.issubset(set(Workout.objects.filter(plan=plan).values_list("id", flat=True)))
        )

    def test_remove_days_only_normalizes_legacy_name_keyed_stored_schedule(self):
        plan = self._create_weekday_plan()
        weekday_names = ("monday", "tuesday", "wednesday", "thursday", "friday")
        legacy_schedule = {weekday_names[int(day)]: day_def for day, day_def in plan.schedule_json.items()}
        WorkoutPlan.objects.filter(id=plan.id).update(schedule_json=legacy_schedule)

        response = self.client.patch(
            self._plan_url(plan),
            {"remove_days": ["friday"]},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        plan.refresh_from_db()
        self.assertEqual(set(plan.schedule_json), {"0", "1", "2", "3"})

    def test_stored_key_normalization_warns_and_keeps_first_duplicate(self):
        with self.assertLogs("apps.fuel.runtime_views", level="WARNING") as captured:
            normalized = _normalize_stored_schedule_keys(
                {
                    "monday": {"activity": "First"},
                    "0": {"activity": "Second"},
                    "funday": {"activity": "Ignored"},
                },
                plan_id="plan-123",
            )

        self.assertEqual(normalized, {"0": {"activity": "First"}})
        self.assertTrue(any("plan-123" in message and "'0'" in message for message in captured.output))
        self.assertTrue(any("plan-123" in message and "'funday'" in message for message in captured.output))

    def test_string_schedule_json_is_rejected(self):
        plan = self._create_weekday_plan()

        response = self.client.patch(
            self._plan_url(plan),
            {"schedule_json": "not-an-object"},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"], "invalid_schedule")
        self.assertEqual(response.data["detail"], "schedule_json must be an object")

    def test_duplicate_numeric_and_name_keys_still_rejected(self):
        plan = self._create_weekday_plan()

        response = self.client.patch(
            self._plan_url(plan),
            {
                "schedule_json": {
                    "4": {
                        "category": "mobility",
                        "activity": "Mobility A",
                        "detail_json": _MOBILITY_PRESCRIPTION,
                    },
                    "friday": {
                        "category": "mobility",
                        "activity": "Mobility B",
                        "detail_json": _MOBILITY_PRESCRIPTION,
                    },
                }
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"], "invalid_schedule")
        self.assertIn("both mean friday", response.data["detail"])
