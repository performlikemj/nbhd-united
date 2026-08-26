"""Runtime contracts for catalog search and catalog-aware plan visibility."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from . import catalog
from .models import Workout, WorkoutPlan


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class CatalogRuntimeCase(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Catalog Runtime", telegram_chat_id=811302)
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def url(self, suffix: str) -> str:
        return f"/api/v1/fuel/runtime/{self.tenant.id}/{suffix}"


class RuntimeFuelExerciseCatalogTests(CatalogRuntimeCase):
    def test_search_returns_names_only_and_no_asset_metadata_anywhere(self):
        response = self.client.get(self.url("exercises/?q=rdl&limit=7"), **self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"][0]["name"], "Romanian Deadlift")
        self.assertGreaterEqual(response.data["total"], 1)
        serialized = repr(response.data).casefold()
        for forbidden in ("slug", "frames", "image", "asset"):
            self.assertNotIn(forbidden, serialized)

    def test_empty_query_returns_default_50_and_facets(self):
        response = self.client.get(self.url("exercises/"), **self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 50)
        self.assertEqual(response.data["total"], 302)
        self.assertEqual(response.data["muscles"], catalog.muscles())
        self.assertEqual(response.data["equipment_types"], catalog.equipment_types())

    def test_filter_is_case_insensitive_and_tolerates_trailing_s(self):
        response = self.client.get(
            self.url("exercises/?muscle=hamstring&equipment=dumbbells&limit=100"),
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.data["total"], 0)
        self.assertTrue(all(row["muscle"] == "Hamstrings" for row in response.data["results"]))
        self.assertTrue(all(row["equipment"] == "Dumbbell" for row in response.data["results"]))

    def test_unknown_filter_is_200_empty_with_legal_lists_and_guidance(self):
        response = self.client.get(self.url("exercises/?q=squat&muscle=Tentacles"), **self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])
        self.assertEqual(response.data["total"], 0)
        self.assertIn("Unknown muscle filter", response.data["guidance"])
        self.assertEqual(response.data["muscles"], catalog.muscles())
        self.assertEqual(response.data["equipment_types"], catalog.equipment_types())

    def test_limit_clamps_to_one_and_one_hundred(self):
        low = self.client.get(self.url("exercises/?limit=0"), **self.headers)
        high = self.client.get(self.url("exercises/?limit=999"), **self.headers)

        self.assertEqual(len(low.data["results"]), 1)
        self.assertEqual(len(high.data["results"]), 100)
        self.assertEqual(low.data["total"], 302)
        self.assertEqual(high.data["total"], 302)

    def test_query_is_truncated_to_80_characters(self):
        with patch("apps.fuel.runtime_views.catalog.search", wraps=catalog.search) as search:
            self.client.get(self.url(f"exercises/?q={'x' * 120}"), **self.headers)
        self.assertEqual(search.call_args.args[0], "x" * 80)

    def test_tenant_person_binding_does_not_rewrite_public_catalog(self):
        self.tenant.pii_entity_map = {"[PERSON_710]": {"name": "Tricep"}}
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["pii_entity_map", "layer1_placeholder_writes"])

        response = self.client.get(self.url("exercises/?q=tricep"), **self.headers)

        names = [row["name"] for row in response.data["results"]]
        self.assertIn("Tricep Pushdown", names)
        self.assertNotIn("[PERSON_710] Pushdown", names)

    def test_internal_auth_is_required(self):
        response = self.client.get(self.url("exercises/?q=bench"))
        self.assertEqual(response.status_code, 401)


class PrescriptionVisibilityTests(CatalogRuntimeCase):
    def setUp(self):
        super().setUp()
        self.plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Visibility",
            start_date=date.today(),
            weeks=1,
            days_per_week=2,
            schedule_json={},
        )
        self.with_detail = Workout.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            date=date.today(),
            status="planned",
            category="strength",
            activity="Detailed",
            detail_json={"exercises": [{"name": "Bench Press", "sets": []}]},
        )
        self.without_detail = Workout.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            date=date.today() + timedelta(days=1),
            status="planned",
            category="strength",
            activity="Empty",
            detail_json={},
        )

    def test_plan_detail_has_true_false_flag_without_detail_json(self):
        response = self.client.get(self.url(f"plans/{self.plan.id}/"), **self.headers)

        by_activity = {row["activity"]: row for row in response.data["workouts"]}
        self.assertIs(by_activity["Detailed"]["has_prescription"], True)
        self.assertIs(by_activity["Empty"]["has_prescription"], False)
        self.assertNotIn("detail_json", by_activity["Detailed"])
        self.assertNotIn("detail_json", by_activity["Empty"])

    @patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"details": {"jobs": []}})
    def test_audit_has_true_false_flag_without_detail_json(self, _gateway):
        response = self.client.get(self.url("audit/"), **self.headers)

        by_activity = {row["activity"]: row for row in response.data["next_14d_workouts"]}
        self.assertIs(by_activity["Detailed"]["has_prescription"], True)
        self.assertIs(by_activity["Empty"]["has_prescription"], False)
        self.assertNotIn("detail_json", by_activity["Detailed"])
        self.assertNotIn("detail_json", by_activity["Empty"])


class UnmatchedExerciseFeedbackTests(CatalogRuntimeCase):
    unknown = "Mystery Curl 9000"

    @property
    def detail(self):
        return {
            "exercises": [
                {"name": "Bench Press", "sets": [{"type": "weighted_reps", "reps": 5, "weight": 50}]},
                {"name": self.unknown, "sets": [{"type": "weighted_reps", "reps": 8, "weight": 10}]},
                {"name": self.unknown, "sets": [{"type": "weighted_reps", "reps": 8, "weight": 10}]},
            ]
        }

    def test_log_workout_warns_deduped_in_order_and_omits_when_empty(self):
        warning = self.client.post(
            self.url("log/"),
            {"activity": "Catalog Check", "category": "strength", "detail_json": self.detail},
            format="json",
            **self.headers,
        )
        clean = self.client.post(
            self.url("log/"),
            {
                "activity": "Bench",
                "category": "strength",
                "detail_json": {"exercises": [self.detail["exercises"][0]]},
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(warning.data["unmatched_exercises"], [self.unknown])
        self.assertNotIn("unmatched_exercises", clean.data)

    def test_update_workout_warns(self):
        workout = Workout.objects.create(
            tenant=self.tenant,
            date=date.today(),
            status="planned",
            category="strength",
            activity="Update",
            detail_json={"exercises": [self.detail["exercises"][0]]},
        )
        response = self.client.patch(
            self.url(f"workouts/{workout.id}/"),
            {"detail_json": self.detail},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.data["unmatched_exercises"], [self.unknown])

    @patch("apps.fuel.runtime_views._manage_fuel_cron")
    def test_create_plan_warns(self, _cron):
        response = self.client.post(
            self.url("plans/"),
            {
                "name": "Unknown accessory",
                "start_date": "2026-04-27",
                "weeks": 1,
                "days_per_week": 1,
                "schedule_json": {"monday": {"activity": "Push", "category": "strength", "detail_json": self.detail}},
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["unmatched_exercises"], [self.unknown])

    @patch("apps.fuel.runtime_views._manage_fuel_cron")
    def test_update_plan_warns(self, _cron):
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Update unknown accessory",
            start_date=date.today() + timedelta(days=7),
            weeks=1,
            days_per_week=1,
            schedule_json={},
        )
        response = self.client.patch(
            self.url(f"plans/{plan.id}/"),
            {"schedule_json": {"monday": {"activity": "Push", "category": "strength", "detail_json": self.detail}}},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["unmatched_exercises"], [self.unknown])

    @patch("apps.fuel.runtime_views._manage_fuel_cron")
    def test_update_plan_warns_from_authored_values_and_guards_egress(self, _cron):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["pii_entity_map", "layer1_placeholder_writes"])
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Authored warning",
            start_date=date.today() + timedelta(days=7),
            weeks=1,
            days_per_week=1,
            schedule_json={},
        )
        detail = {
            "exercises": [
                {
                    "name": "Alice special",
                    "sets": [{"type": "bodyweight_reps", "reps": 8}],
                }
            ]
        }

        response = self.client.patch(
            self.url(f"plans/{plan.id}/"),
            {"schedule_json": {"monday": {"activity": "Push", "category": "strength", "detail_json": detail}}},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["unmatched_exercises"], ["[PERSON_1] special"])
        self.assertNotIn("Alice", repr(response.data["unmatched_exercises"]))
        plan.refresh_from_db()
        self.assertEqual(
            plan.schedule_json["0"]["detail_json"]["exercises"][0]["name"],
            "[PERSON_1] special",
        )
