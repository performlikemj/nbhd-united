"""Segment writes preserve legacy contracts and durable prescription estimates."""

import copy
from datetime import UTC, date, datetime

from django.test import SimpleTestCase, TestCase

from apps.fuel.cardio import materialize_prescription
from apps.fuel.models import Workout, WorkoutPlan, WorkoutTemplate
from apps.fuel.serializers import WorkoutSerializer, WorkoutTemplateSerializer
from apps.fuel.set_contract import normalize_detail, validate_cardio_prescription, validate_detail, validate_flat_detail
from apps.fuel.test_cardio_segments import EXAMPLES, CardioRuntimeWriteTests


def estimated_detail():
    return {**copy.deepcopy(EXAMPLES["intervals_mixed"]), "planned": {"duration_s": 2700}}


class CardioLegacyCompatibilityTests(SimpleTestCase):
    def test_duration_and_category_only_legacy_edits_do_not_inject_detail(self):
        for detail in ({"exercises": None}, {"exercises": [{"name": "Legacy", "sets": [{"reps": -100}]}]}):
            row = Workout(category="other", detail_json=detail, duration_minutes=45)
            for payload in ({"duration_minutes": 45}, {"category": "strength"}, {"category": "cardio"}):
                with self.subTest(detail=detail, payload=payload):
                    serializer = WorkoutSerializer(row, data=payload, partial=True)
                    self.assertTrue(serializer.is_valid(), serializer.errors)
                    self.assertNotIn("detail_json", serializer.validated_data)
                    self.assertEqual(row.detail_json, detail)

    def test_notes_and_completed_duration_keep_historical_plan(self):
        for status in ("planned", "done", "skipped"):
            row = Workout(category="cardio", status=status, detail_json=estimated_detail(), duration_minutes=45)
            serializer = WorkoutSerializer(
                row, data={"detail_json": {**row.detail_json, "notes": "changed"}}, partial=True
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertEqual(serializer.validated_data["detail_json"]["planned"], {"duration_s": 2700})
            self.assertNotIn("duration_minutes", serializer.validated_data)
            if status != "planned":
                serializer = WorkoutSerializer(row, data={"duration_minutes": 32}, partial=True)
                self.assertTrue(serializer.is_valid(), serializer.errors)
                self.assertNotIn("detail_json", serializer.validated_data)
                self.assertEqual(row.detail_json["planned"], {"duration_s": 2700})

    def test_done_creation_does_not_treat_actual_duration_as_plan(self):
        serializer = WorkoutSerializer(
            data={
                "category": "cardio",
                "activity": "Run",
                "date": "2099-01-05",
                "status": "done",
                "duration_minutes": 32,
                "detail_json": EXAMPLES["intervals_mixed"],
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["detail_json"]["planned"], {})
        self.assertEqual(serializer.validated_data["duration_minutes"], 32)

    def test_legacy_terrain_and_planned_extension_survive(self):
        detail = {"terrain": "grass", "planned": {"notes": {"text": "extension"}}}
        for category in ("strength", "other", "cardio"):
            for validator in (validate_detail, validate_flat_detail):
                self.assertIsNone(validator(detail, category)[1])
            self.assertEqual(normalize_detail(detail, category)[0], detail)
            serializer = WorkoutSerializer(
                data={"category": category, "activity": "Activity", "date": "2099-01-05", "detail_json": detail}
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertEqual(serializer.validated_data["detail_json"], detail)
        self.assertEqual(normalize_detail({"planned": detail["planned"]}, "cardio")[0]["planned"], detail["planned"])

    def test_segment_free_cardio_terrain_survives_template_echo_and_category_correction(self):
        detail = {"terrain": "grass", "distance_km": 5}
        self.assertEqual(validate_cardio_prescription(detail), [])
        template = WorkoutTemplate(category="cardio", detail_json=detail)
        echo = WorkoutTemplateSerializer(template, data={"detail_json": detail}, partial=True)
        self.assertTrue(echo.is_valid(), echo.errors)
        self.assertEqual(echo.validated_data["detail_json"], detail)
        row = Workout(category="other", detail_json=detail)
        correction = WorkoutSerializer(row, data={"category": "cardio"}, partial=True)
        self.assertTrue(correction.is_valid(), correction.errors)
        self.assertEqual(correction.validated_data, {"category": "cardio"})

    def test_template_category_changes_validate_segments_in_both_directions(self):
        for old_category, new_category, detail, expected in (
            ("cardio", "strength", EXAMPLES["easy_run_timed"], False),
            ("cardio", "other", EXAMPLES["easy_run_timed"], False),
            ("other", "cardio", EXAMPLES["easy_run_timed"], True),
            ("other", "cardio", {"segments": []}, False),
        ):
            with self.subTest(old=old_category, new=new_category, detail=detail):
                row = WorkoutTemplate(category=old_category, detail_json=detail)
                serializer = WorkoutTemplateSerializer(row, data={"category": new_category}, partial=True)
                self.assertEqual(serializer.is_valid(), expected, serializer.errors)
                if not expected:
                    self.assertIn("segments", str(serializer.errors))
        row = WorkoutTemplate(category="cardio", detail_json=EXAMPLES["easy_run_timed"])
        removal = WorkoutTemplateSerializer(row, data={"category": "other", "detail_json": {}}, partial=True)
        self.assertTrue(removal.is_valid(), removal.errors)

    def test_grandfathered_terrain_cannot_hide_new_negative_reps(self):
        stored = {
            "terrain": "grass",
            "exercises": [{"name": "Custom lift", "sets": [{"type": "weighted_reps", "reps": 5, "weight": 10}]}],
        }
        incoming = copy.deepcopy(stored)
        incoming["exercises"][0]["sets"][0]["reps"] = -100
        serializer = WorkoutSerializer(
            Workout(category="strength", detail_json=stored), data={"detail_json": incoming}, partial=True
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("reps", str(serializer.errors))
        _, error = validate_detail({**incoming, "segments": []}, "strength")
        self.assertTrue(any(e["type"] == "cardio_category_invalid" for e in error.details))
        self.assertTrue(any("reps" in e["loc"] for e in error.details))

    def test_legacy_template_does_not_acquire_general_validation(self):
        detail = {"distance_km": "5 miles"}
        row = WorkoutTemplate(category="cardio", detail_json=detail)
        for payload in ({"duration_minutes": 45}, {"detail_json": detail}, {"name": "Run"}):
            serializer = WorkoutTemplateSerializer(row, data=payload, partial=True)
            self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertEqual(serializer.validated_data, payload)

    def test_materialization_does_not_normalize_legacy_strength_specs(self):
        from apps.fuel.services import SlotKey, WorkoutSpec

        detail = {"exercises": [{"name": "Push-up", "sets": [{"reps": 10}]}]}
        fields = {"category": "strength", "detail_json": detail, "duration_minutes": 45}
        self.assertEqual(materialize_prescription(fields), fields)
        spec = WorkoutSpec(SlotKey(0, 0), date(2099, 1, 5), "strength", "Push-up", 45, detail)
        self.assertEqual((spec.category, spec.detail_json), ("strength", detail))


class CardioRuntimeCompatibilityTests(TestCase):
    setUp = CardioRuntimeWriteTests.setUp

    def test_duration_only_and_category_only_legacy_patches(self):
        row = Workout.objects.create(
            tenant=self.tenant,
            date=date(2099, 1, 5),
            category="other",
            activity="Legacy",
            status="planned",
            detail_json={"exercises": None},
        )
        url = self.base + f"workouts/{row.id}/"
        for payload in (
            {"detail_json": None},
            {"duration_minutes": 45},
            {"category": "strength"},
            {"category": "cardio"},
        ):
            response = self.client.patch(url, payload, format="json", **self.headers)
            self.assertEqual(response.status_code, 200, response.data)
            row.refresh_from_db()
            self.assertEqual(row.detail_json, {"exercises": None})

    def test_segment_free_cardio_terrain_survives_runtime_note_edit(self):
        detail = {"terrain": "grass", "distance_km": 5}
        row = Workout.objects.create(
            tenant=self.tenant,
            date=date(2099, 1, 5),
            category="cardio",
            activity="Run",
            status="planned",
            detail_json=detail,
        )
        response = self.client.patch(
            self.base + f"workouts/{row.id}/",
            {"detail_json": {**detail, "notes": "changed"}},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 200, response.data)
        row.refresh_from_db()
        self.assertEqual(row.detail_json, {**detail, "notes": "changed"})

    def test_activity_only_plan_edit_preserves_inherited_cardio_terrain(self):
        from unittest.mock import patch

        detail = {"terrain": "grass", "distance_km": 5}
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Legacy terrain",
            start_date=date(2099, 1, 5),
            weeks=1,
            days_per_week=1,
            schedule_json={"0": {"category": "cardio", "activity": "Run", "detail_json": detail}},
        )
        with patch("apps.fuel.runtime_views._manage_fuel_cron"):
            response = self.client.patch(
                self.base + f"plans/{plan.id}/",
                {"schedule_json": {"monday": {"activity": "Easy run"}}},
                format="json",
                **self.headers,
            )
        self.assertEqual(response.status_code, 200, response.data)
        plan.refresh_from_db()
        self.assertEqual(plan.schedule_json["0"]["detail_json"], detail)
        self.assertEqual(plan.schedule_json["0"]["activity"], "Easy run")

    def test_owner_weeks_only_regeneration_sanitizes_stored_categories(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=self.tenant.user)
        for category in ("legacy-run", None):
            with self.subTest(category=category):
                detail = {"terrain": "grass", "distance_km": 5}
                plan = WorkoutPlan.objects.create(
                    tenant=self.tenant,
                    name="Legacy category",
                    start_date=date(2099, 1, 5),
                    weeks=1,
                    days_per_week=1,
                    schedule_json={"0": {"category": category, "activity": "Run", "detail_json": detail}},
                )
                response = client.patch(f"/api/v1/fuel/plans/{plan.id}/", {"weeks": 2}, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                workouts = list(Workout.objects.filter(plan=plan))
                self.assertEqual(len(workouts), 2)
                for workout in workouts:
                    self.assertEqual(workout.category, "other")
                    self.assertEqual(workout.detail_json, detail)
                plan.refresh_from_db()
                self.assertEqual(plan.schedule_json["0"]["category"], category)

    def test_clearing_timed_segments_fails_final_prescription_guard(self):
        row = Workout.objects.create(
            tenant=self.tenant,
            date=date(2099, 1, 5),
            category="cardio",
            activity="Run",
            status="planned",
            duration_minutes=35,
            detail_json={**EXAMPLES["easy_run_timed"], "planned": {"duration_s": 2100}},
        )
        response = self.client.patch(
            self.base + f"workouts/{row.id}/", {"detail_json": {}}, format="json", **self.headers
        )
        self.assertEqual(response.status_code, 400, response.data)
        row.refresh_from_db()
        self.assertEqual(row.duration_minutes, 35)
        self.assertIn("segments", row.detail_json)

    def test_runtime_notes_and_done_actual_edits_preserve_plan(self):
        for status in ("planned", "done"):
            row = Workout.objects.create(
                tenant=self.tenant,
                date=date(2099, 1, 5),
                category="cardio",
                activity="Run",
                status=status,
                duration_minutes=45,
                detail_json=estimated_detail(),
            )
            url = self.base + f"workouts/{row.id}/"
            response = self.client.patch(
                url, {"detail_json": {**row.detail_json, "notes": "changed"}}, format="json", **self.headers
            )
            self.assertEqual(response.status_code, 200, response.data)
            row.refresh_from_db()
            self.assertEqual(row.detail_json["planned"], {"duration_s": 2700})
            self.assertEqual(row.duration_minutes, 45)
            if status == "done":
                response = self.client.patch(url, {"duration_minutes": 32}, format="json", **self.headers)
                self.assertEqual(response.status_code, 200, response.data)
                row.refresh_from_db()
                self.assertEqual(row.detail_json["planned"], {"duration_s": 2700})
                self.assertEqual(row.duration_minutes, 32)

    def test_healthkit_ignores_planned_extension_without_cardio_segments(self):
        from apps.fuel.healthkit import _find_candidate

        started = datetime(2099, 1, 5, 10, tzinfo=UTC)
        for category, duration, planned_seconds, expected in (
            ("strength", None, 3600, True),
            ("cardio", 30, 60, False),
        ):
            row = Workout.objects.create(
                tenant=self.tenant,
                date=started.date(),
                category=category,
                activity="Exercise",
                status="planned",
                duration_minutes=duration,
                detail_json={"planned": {"duration_s": planned_seconds}},
            )
            clean = {
                "category": category,
                "raw_type": "running",
                "started_at": started,
                "duration_minutes": 3,
                "metrics": {},
            }
            self.assertEqual(_find_candidate(self.tenant, clean, UTC, set()) is not None, expected)
            row.delete()
