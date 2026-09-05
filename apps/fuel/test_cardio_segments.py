"""Shared fixture and write-path contracts for typed cardio prescriptions."""

import copy
import json
from pathlib import Path
from unittest import TestCase

from django.test import TestCase as DjangoTestCase

from apps.common import llm_lookups
from apps.fuel.set_contract import (
    derive_planned,
    expand_cardio_reps,
    has_prescription,
    normalize_detail,
    split_detail_errors,
    validate_cardio_prescription,
    validate_detail,
    validate_flat_detail,
)

FIXTURE = json.loads((Path(__file__).resolve().parents[2] / "contracts/fuel_cardio_segments.v1.json").read_text())
EXAMPLES = {example["name"]: example["detail_json"] for example in FIXTURE["examples"]}


class CardioContractTests(TestCase):
    def test_vocabulary_and_limits_match_fixture(self):
        for key in ("kinds", "efforts", "recovery_efforts", "terrains"):
            self.assertEqual(list(getattr(llm_lookups, "CARDIO_" + key.upper())), FIXTURE[key])
        self.assertEqual(llm_lookups.CARDIO_LIMITS, FIXTURE["limits"])
        self.assertEqual(llm_lookups.CARDIO_PACE_REGEX, FIXTURE["pace_regex"])

    def test_examples(self):
        for example in FIXTURE["examples"]:
            with self.subTest(example["name"]):
                detail = example["detail_json"]
                expected = example["expected"]
                self.assertEqual(validate_cardio_prescription(detail), [])
                for validate in (validate_detail, validate_flat_detail):
                    self.assertIsNone(validate(detail, "cardio")[1])
                self.assertEqual(derive_planned(detail["segments"]), expected["planned"])
                self.assertEqual(normalize_detail(detail, "cardio")[0]["planned"], expected["planned"])
                self.assertEqual(expand_cardio_reps(detail["segments"]), expected["expanded_reps"])
                self.assertEqual(has_prescription(detail, "cardio"), expected["has_prescription"])

    def test_invalid_fixture_rejected_by_both_paths(self):
        for example in FIXTURE["invalid"]:
            for validate in (validate_detail, validate_flat_detail):
                with self.subTest(example["name"], validator=validate.__name__):
                    self.assertIsNotNone(validate(example["detail_json"], "cardio")[1])

    def test_strict_numbers_and_limits(self):
        block = {"kind": "steady", "duration_s": 60, "effort": "easy"}
        for value in (True, "60", 60.5, 9, 14401, None):
            with self.subTest(value=value):
                self.assertTrue(validate_cardio_prescription({"segments": [{**block, "duration_s": value}]}))
        for value in (True, "1", float("nan"), float("inf"), 0.049, 101):
            self.assertTrue(
                validate_cardio_prescription({"segments": [{"kind": "steady", "distance_km": value, "effort": "easy"}]})
            )
        self.assertTrue(validate_cardio_prescription({"segments": [block] * 41}))
        self.assertTrue(validate_cardio_prescription({"segments": None}))
        self.assertTrue(validate_cardio_prescription({"terrain": "sand"}))

    def test_server_owned_planned_and_explicit_duration(self):
        detail = {**EXAMPLES["intervals_mixed"], "planned": {"duration_s": 999}}
        self.assertEqual(normalize_detail(detail, "cardio")[0]["planned"], {})
        self.assertEqual(
            normalize_detail(detail, "cardio", explicit_duration_minutes=45)[0]["planned"], {"duration_s": 2700}
        )
        self.assertNotIn("planned", normalize_detail({"planned": {"distance_km": 99}}, "cardio")[0])
        self.assertEqual(detail["planned"], {"duration_s": 999})
        self.assertEqual(
            derive_planned(
                [
                    {
                        "kind": "interval",
                        "repeat": 3,
                        "distance_km": 1,
                        "effort": "hard",
                        "recovery": {"distance_km": 0.2, "effort": "easy"},
                    }
                ]
            ),
            {"distance_km": 3.4},
        )

    def test_cardio_only(self):
        for validate in (validate_detail, validate_flat_detail):
            self.assertIn(
                "only valid for cardio", validate(EXAMPLES["easy_run_timed"], "strength")[1].details[0]["msg"]
            )

    def test_grandfathering_is_fragment_based(self):
        stored = copy.deepcopy(FIXTURE["invalid"][0]["detail_json"])
        incoming = {**stored, "notes": "updated"}
        error = validate_detail(incoming, "cardio")[1]
        self.assertEqual(split_detail_errors(error.details, incoming, stored)[0], [])
        incoming = copy.deepcopy(incoming)
        incoming["segments"][0]["duration_s"] = 700
        self.assertTrue(split_detail_errors(error.details, incoming, stored)[0])


class CardioWriteValidationTests(TestCase):
    def test_plan_and_week_overrides_accept_mixed(self):
        from apps.fuel.runtime_views import _validate_normalize_schedule, _validate_normalize_week_overrides

        day = {"category": "cardio", "activity": "Run", "detail_json": EXAMPLES["intervals_mixed"]}
        self.assertIsNone(_validate_normalize_schedule({"0": day})[1])
        self.assertIsNone(_validate_normalize_week_overrides({"0": {"0": day}}, weeks=1)[1])

    def test_template_validates_and_derives(self):
        from apps.fuel.serializers import WorkoutTemplateSerializer

        serializer = WorkoutTemplateSerializer(
            data={"name": "Run", "category": "cardio", "detail_json": EXAMPLES["intervals_mixed"]}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["detail_json"]["planned"], {})
        serializer = WorkoutTemplateSerializer(
            data={"name": "Run", "category": "cardio", "detail_json": {"segments": []}}
        )
        self.assertFalse(serializer.is_valid())

    def test_category_only_owner_patch_revalidates_stored_detail(self):
        from apps.fuel.models import Workout
        from apps.fuel.serializers import WorkoutSerializer

        instance = Workout(category="other", detail_json={"segments": []})
        for payload in ({"category": "cardio"}, {"category": "cardio", "detail_json": instance.detail_json}):
            serializer = WorkoutSerializer(instance, data=payload, partial=True)
            self.assertFalse(serializer.is_valid())


class CardioRuntimeCategoryTests(DjangoTestCase):
    def test_category_only_runtime_patch_revalidates(self):
        from datetime import date
        from unittest.mock import patch

        from rest_framework.test import APIRequestFactory

        from apps.fuel.models import Workout
        from apps.fuel.runtime_views import RuntimeWorkoutDetailView
        from apps.tenants.services import create_tenant

        tenant = create_tenant(display_name="Cardio", telegram_chat_id=812908)
        workout = Workout.objects.create(
            tenant=tenant, category="other", activity="Run", date=date.today(), detail_json={"segments": []}
        )
        request = APIRequestFactory().patch("/", {"category": "cardio"}, format="json")
        with patch.object(RuntimeWorkoutDetailView, "_get_workout", return_value=(tenant, workout, None)):
            response = RuntimeWorkoutDetailView.as_view()(request, tenant_id=tenant.id, workout_id=workout.id)
        self.assertEqual(response.status_code, 400)
        workout.refresh_from_db()
        self.assertEqual(workout.category, "other")
