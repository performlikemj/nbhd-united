from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .models import WorkoutPlan
from .set_contract import validate_detail


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class PlanVarietyRuntimeTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Plan Variety", telegram_chat_id=811303)
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        self.start = date.today() + timedelta(days=((7 - date.today().weekday()) % 7) or 7)

    def url(self, suffix=""):
        return f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{suffix}"

    def item(self, name="Bench Press", *, weight=60, role=None):
        item = {
            "name": name,
            "sets": [{"type": "weighted_reps", "reps": 8, "weight": weight}],
        }
        if role:
            item["role"] = role
        return item

    def day(self, activity, *items):
        return {
            "activity": activity,
            "category": "strength",
            "detail_json": {"exercises": list(items or (self.item(),))},
        }

    def body(self, schedule, **extra):
        body = {
            "name": extra.pop("name", "Eight week block"),
            "start_date": self.start.isoformat(),
            "weeks": 8,
            "days_per_week": len(schedule),
            "schedule_json": schedule,
        }
        body.update(extra)
        return body

    @patch("apps.fuel.runtime_views._manage_fuel_cron")
    def test_five_identical_tracks_reject_and_name_all_five(self, _cron):
        schedule = {
            weekday: self.day(activity)
            for weekday, activity in zip(
                ("monday", "tuesday", "wednesday", "thursday", "friday"),
                ("Push", "Pull", "Legs", "Upper", "Lower"),
                strict=True,
            )
        }
        response = self.client.post(self.url(), self.body(schedule), format="json", **self.headers)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"], "plan_rotation_required")
        self.assertEqual(len(response.data["tracks"]), 5)
        self.assertEqual({track["weekday"] for track in response.data["tracks"]}, set(schedule))
        self.assertTrue(all(track["weeks"] == list(range(1, 9)) for track in response.data["tracks"]))
        self.assertTrue(all(track["max_consecutive_same"] == 8 for track in response.data["tracks"]))
        self.assertEqual(response.data["week_overrides_semantics"], "whole_map_replacement")
        self.assertEqual(response.data["catalog_candidates"], [])
        self.assertFalse(WorkoutPlan.objects.filter(tenant=self.tenant).exists())

    @patch("apps.fuel.runtime_views._manage_fuel_cron")
    def test_one_accessory_rotation_still_rejects_the_other_tracks(self, _cron):
        weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday")
        schedule = {day: self.day(day.title(), self.item("Hammer Curl", role="accessory")) for day in weekdays}
        rotated = self.day("Monday", self.item("Front Raise", role="accessory"))
        overrides = {str(week): {"monday": rotated} for week in (2, 3, 6, 7)}

        response = self.client.post(
            self.url(),
            self.body(schedule, week_overrides=overrides),
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual({track["weekday"] for track in response.data["tracks"]}, set(weekdays[1:]))

    @patch("apps.fuel.runtime_views._manage_fuel_cron")
    def test_rest_override_does_not_reset_the_run(self, _cron):
        response = self.client.post(
            self.url(),
            self.body({"monday": self.day("Push")}, week_overrides={"3": {"monday": None}}),
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["tracks"][0]["weeks"], [1, 2, 3, 5, 6, 7, 8])
        self.assertEqual(response.data["tracks"][0]["max_consecutive_same"], 7)

    @patch("apps.fuel.runtime_views._manage_fuel_cron")
    def test_load_only_change_needs_validated_progression_policy(self, _cron):
        override = {"3": {"monday": self.day("Push", self.item(weight=50))}}
        body = self.body({"monday": self.day("Push")}, week_overrides=override)

        rejected = self.client.post(self.url(), body, format="json", **self.headers)
        self.assertEqual(rejected.status_code, 400, rejected.data)

        accepted = self.client.post(
            self.url(),
            {**body, "variation_policy": "progression_only"},
            format="json",
            **self.headers,
        )
        self.assertEqual(accepted.status_code, 201, accepted.data)
        plan = WorkoutPlan.objects.get(id=accepted.data["id"])
        self.assertEqual(plan.schedule_json["_plan_policy"], {"variation_policy": "progression_only"})
        self.assertNotIn("_plan_policy", accepted.data["schedule_json"])
        self.assertEqual(accepted.data["variation_policy"], "progression_only")

    @patch("apps.fuel.runtime_views._manage_fuel_cron")
    def test_intentional_rehab_repeat_passes_and_is_stored(self, _cron):
        response = self.client.post(
            self.url(),
            self.body(
                {"monday": self.day("Rehab", self.item("Bodyweight Squat"))},
                repeat_policy="intentional",
                repeat_reason="Clinician-directed fixed rehab block",
            ),
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["repeat_policy"], "intentional")
        self.assertEqual(response.data["repeat_reason"], "Clinician-directed fixed rehab block")
        plan = WorkoutPlan.objects.get(id=response.data["id"])
        self.assertEqual(plan.schedule_json["_plan_policy"]["repeat_policy"], "intentional")

    @patch("apps.fuel.runtime_views._manage_fuel_cron")
    def test_metadata_only_patch_never_retroactively_rejects_legacy_plan(self, _cron):
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Legacy repeated",
            start_date=self.start,
            weeks=8,
            days_per_week=1,
            schedule_json={"0": self.day("Push")},
        )
        schedule_before = plan.schedule_json

        response = self.client.patch(
            self.url(f"{plan.id}/"),
            {"notes": "Metadata only"},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 200, response.data)
        plan.refresh_from_db()
        self.assertEqual(plan.schedule_json, schedule_before)

    @patch("apps.fuel.runtime_views._manage_fuel_cron")
    def test_weeks_schedule_and_override_patches_each_run_the_guard(self, _cron):
        cases = (
            (4, {"weeks": 8}),
            (8, {"schedule_json": {"monday": {"activity": "Push"}}}),
            (8, {"week_overrides": {"3": {"monday": None}}}),
        )
        for index, (weeks, patch_body) in enumerate(cases):
            plan = WorkoutPlan.objects.create(
                tenant=self.tenant,
                name=f"Legacy repeated {index}",
                start_date=self.start,
                weeks=weeks,
                days_per_week=1,
                schedule_json={"0": self.day("Push")},
            )
            response = self.client.patch(
                self.url(f"{plan.id}/"),
                patch_body,
                format="json",
                **self.headers,
            )
            self.assertEqual(response.status_code, 400, (patch_body, response.data))
            self.assertEqual(response.data["error"], "plan_rotation_required")
            plan.refresh_from_db()
            self.assertEqual(plan.weeks, weeks)

    @patch("apps.fuel.runtime_views._manage_fuel_cron")
    def test_candidates_are_accessory_only_and_globally_capped(self, _cron):
        items = tuple(self.item("Arnold Press", role="accessory") for _ in range(4))
        schedule = {day: self.day(day.title(), *items) for day in ("monday", "tuesday", "wednesday", "thursday")}
        response = self.client.post(self.url(), self.body(schedule), format="json", **self.headers)

        self.assertEqual(response.status_code, 400, response.data)
        candidates = response.data["catalog_candidates"]
        self.assertTrue(all(len(candidate["names"]) <= 6 for candidate in candidates))
        names = [name for candidate in candidates for name in candidate["names"]]
        self.assertLessEqual(len(names), 18)
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(candidate["loc"][-1] == "name" for candidate in candidates))


class ExerciseRoleContractTests(TestCase):
    def test_invalid_role_is_rejected_for_strength_and_mobility(self):
        for category, container in (("strength", "exercises"), ("mobility", "skills")):
            _detail, error = validate_detail(
                {container: [{"name": "Bench Press", "role": "secondary", "sets": []}]},
                category,
            )
            self.assertIsNotNone(error)
            self.assertEqual(error.details[0]["type"], "invalid_role")
