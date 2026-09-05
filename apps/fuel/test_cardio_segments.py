"""Shared fixture and write-path contracts for typed cardio prescriptions."""

import copy
import json
from datetime import UTC
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


class CardioMaterializationTests(DjangoTestCase):
    def setUp(self):
        from datetime import date

        from apps.fuel.models import WorkoutPlan
        from apps.tenants.services import create_tenant

        self.today = date(2026, 9, 7)
        self.tenant = create_tenant(display_name="Cardio plan", telegram_chat_id=812909)
        self.plan = WorkoutPlan.objects.create(tenant=self.tenant, name="Runs", start_date=self.today, weeks=1)

    def day(self, name="easy_run_timed", **extra):
        return {"category": "cardio", "activity": "Run", "detail_json": copy.deepcopy(EXAMPLES[name]), **extra}

    def reconcile(self, day, **kwargs):
        from apps.fuel.services import apply_reconciliation, reconcile_plan_state

        rec = reconcile_plan_state(self.plan, {"0": day}, 1, today=self.today)
        return apply_reconciliation(rec, plan=self.plan, tenant=self.tenant, **kwargs)

    def test_initial_expansion_and_counter(self):
        from apps.fuel.models import Workout
        from apps.fuel.runtime_views import _author_plan_expansion_inputs, _expand_plan_workouts
        from apps.platform_logs.models import ToolContractEvent

        schedule = {"0": self.day()}
        authored = _author_plan_expansion_inputs(self.tenant, schedule, 1, writer="runtime")
        _expand_plan_workouts(self.plan, self.tenant, schedule, self.today, 1, authored_workouts=authored)
        workout = Workout.objects.get(plan=self.plan)
        self.assertEqual(workout.duration_minutes, 35)
        self.assertEqual(workout.detail_json["planned"], {"duration_s": 2100})
        event = ToolContractEvent.objects.get(tool_name="fuel.cardio.prescription_shape")
        self.assertEqual(event.reason_code, "segments")
        self.assertNotIn("dropped_keys", event.detail)

    def test_reconciliation_create_retemplate_and_omission(self):
        from apps.fuel.models import Workout

        self.reconcile(self.day())
        workout = Workout.objects.get(plan=self.plan)
        self.assertEqual(workout.duration_minutes, 35)
        self.reconcile({"category": "cardio"})
        workout.refresh_from_db()
        self.assertEqual(workout.duration_minutes, 35)
        self.reconcile(self.day("intervals_mixed"))
        workout.refresh_from_db()
        self.assertIsNone(workout.duration_minutes)
        self.assertEqual(workout.detail_json["planned"], {})
        self.reconcile(self.day("intervals_mixed", duration_minutes=45))
        workout.refresh_from_db()
        self.assertEqual(workout.duration_minutes, 45)
        self.assertEqual(workout.detail_json["planned"], {"duration_s": 2700})

    def test_adoption_and_lock_preservation(self):
        from apps.fuel.models import Workout

        workout = Workout.objects.create(
            tenant=self.tenant, plan=self.plan, date=self.today, category="cardio", activity="Run", status="planned"
        )
        self.reconcile(self.day())
        workout.refresh_from_db()
        self.assertIsNotNone(workout.slot_id)
        self.assertEqual(workout.duration_minutes, 35)
        counts = self.reconcile(self.day("intervals_mixed"), edit_lock_check=lambda _: True)
        self.assertEqual(counts["workouts_locked_skip"], 1)
        workout.refresh_from_db()
        self.assertEqual(workout.duration_minutes, 35)

    def test_helper_ceil_removal_duration_edit_and_no_mutation(self):
        from apps.fuel.cardio import materialize_prescription

        detail = {"segments": [{"kind": "steady", "duration_s": 61, "effort": "easy"}]}
        fields = materialize_prescription({"category": "cardio", "detail_json": detail})
        self.assertEqual(fields["duration_minutes"], 2)
        self.assertEqual(fields["detail_json"]["planned"], {"duration_s": 61})
        self.assertNotIn("planned", detail)
        removed = materialize_prescription(
            {"detail_json": {"structure": "easy run"}},
            category="cardio",
            stored_detail=fields["detail_json"],
            stored_duration=2,
        )
        self.assertIsNone(removed["duration_minutes"])
        edited = materialize_prescription({"duration_minutes": 45}, category="cardio", stored_detail=detail)
        self.assertEqual(edited["detail_json"]["planned"], {"duration_s": 2700})


class CardioHealthKitTests(DjangoTestCase):
    def test_match_gates_and_preserved_prescription(self):
        from datetime import datetime

        from apps.fuel.cardio import materialize_prescription
        from apps.fuel.healthkit import _complete_planned, _find_candidate
        from apps.fuel.models import Workout
        from apps.tenants.services import create_tenant

        tenant = create_tenant(display_name="Cardio Health", telegram_chat_id=812910)
        started = datetime(2026, 9, 7, 8, tzinfo=UTC)
        cases = [
            ("intervals_mixed", None, 3, 0.43, False),
            ("intervals_mixed", 45, 32, 5.1, True),
            ("long_run_distance", None, 30, 5, False),
            ("long_run_distance", None, 60, 9, True),
            ("long_run_distance", None, 60, None, False),
            ("easy_run_timed", None, 30, 5.1, True),
        ]
        for name, duration, actual_minutes, distance, expected in cases:
            with self.subTest(name=name, distance=distance, duration=duration):
                fields = materialize_prescription(
                    {"category": "cardio", "detail_json": EXAMPLES[name], "duration_minutes": duration}
                )
                fields["detail_json"]["structure"] = "legacy caption"
                workout = Workout.objects.create(
                    tenant=tenant, activity="Run", date=started.date(), status="planned", **fields
                )
                clean = {
                    "started_at": started,
                    "category": "cardio",
                    "raw_type": "running",
                    "duration_minutes": actual_minutes,
                    "duration_seconds": actual_minutes * 60,
                    "external_id": "cardio-test",
                    "metrics": {"distance_km": distance},
                }
                match = _find_candidate(tenant, clean, UTC, set())
                self.assertEqual(match is not None, expected)
                if expected:
                    prior = copy.deepcopy(workout.detail_json)
                    receipt = {"status": "checked", "redactions": []}
                    _complete_planned(
                        workout,
                        clean,
                        {
                            "activity": "Run",
                            "detail_json": {"distance_km": distance, "avg_hr": 145, "planned": {"duration_s": 1}},
                        },
                        {"activity": receipt, "detail_json": receipt},
                    )
                    workout.refresh_from_db()
                    for key in ("segments", "planned", "terrain", "structure"):
                        self.assertEqual(workout.detail_json.get(key), prior.get(key))
                    self.assertEqual(workout.detail_json["distance_km"], distance)
                    self.assertEqual(workout.detail_json["avg_hr"], 145)
                    self.assertEqual(workout.duration_seconds, actual_minutes * 60)
                workout.delete()


class CardioRuntimeWriteTests(DjangoTestCase):
    def setUp(self):
        from django.test import override_settings
        from rest_framework.test import APIClient

        from apps.tenants.services import create_tenant
        from apps.tenants.test_utils import seed_internal_key

        override = override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
        override.enable()
        self.addCleanup(override.disable)
        self.tenant = create_tenant(display_name="Cardio write", telegram_chat_id=812912)
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {"HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key", "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id)}
        self.base = f"/api/v1/fuel/runtime/{self.tenant.id}/"

    def test_planned_legacy_log_warns_but_done_log_does_not(self):
        for state, expected in (("planned", True), ("done", False)):
            response = self.client.post(
                self.base + "workouts/",
                {
                    "category": "cardio",
                    "activity": "Run",
                    "status": state,
                    "detail_json": {"exercises": [{"name": "Run"}]},
                },
                format="json",
                **self.headers,
            )
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual("warnings" in response.data, expected)
            if expected:
                self.assertEqual(response.data["warnings"], ["cardio days use segments, not exercises"])

    def test_plan_day_warning_and_segment_plan_acceptance(self):
        from unittest.mock import patch

        for detail, warning in (({"exercises": [{"name": "Run"}]}, True), (EXAMPLES["intervals_mixed"], False)):
            with patch("apps.fuel.runtime_views._manage_fuel_cron"):
                response = self.client.post(
                    self.base + "plans/",
                    {
                        "name": "Runs",
                        "start_date": "2099-01-05",
                        "weeks": 1,
                        "days_per_week": 1,
                        "schedule_json": {"monday": {"category": "cardio", "activity": "Run", "detail_json": detail}},
                    },
                    format="json",
                    **self.headers,
                )
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual("warnings" in response.data, warning)
