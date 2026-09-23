"""Per-set actuals remain distinct from prescriptions through real write paths."""

import copy
import json
from datetime import date, timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from pydantic import ValidationError
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.fuel.models import Workout, WorkoutPlan
from apps.fuel.set_contract import (
    BodyweightRepsSet,
    HoldTimeSet,
    WeightedRepsSet,
    coerce_set,
    logged_detail_errors,
    logged_sets_summary,
    normalize_detail,
    preserve_logged_sets,
    validate_detail,
    validate_flat_detail,
)
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

AT = "2026-09-23T07:12:03Z"
CASES = (
    ("Bench press", {"type": "weighted_reps", "reps": 8, "weight": 60}, {"reps": 8, "weight": 62.5, "at": AT}),
    ("Push-up", {"type": "bodyweight_reps", "reps": 10}, {"reps": 12, "at": AT}),
    ("Plank", {"type": "hold_time", "hold_s": 30}, {"hold_s": 45, "at": AT}),
)


def detail_for(name, prescription, logged):
    return {"exercises": [{"name": name, "sets": [{**prescription, "logged": logged}]}]}


def invalid_cases():
    for name, prescription, good in CASES:
        for key in set(good) - {"at"}:
            for bad in (-1, True, False, "8", None, [], {}, float("nan"), float("inf"), -float("inf")):
                yield name, prescription, {**good, key: bad}
            if key != "weight":
                for bad in (1.5, 8.0):
                    yield name, prescription, {**good, key: bad}
            yield name, prescription, {k: v for k, v in good.items() if k != key}
        for bad in (
            None,
            True,
            123,
            "",
            "2026-09-23",
            "2026-09-23T07:12:03",
            "2026-09-23T07:12:03+09:00",
            "2026-02-30T07:12:03Z",
            "2026-09-23T25:12:03Z",
            "2026-09-23 07:12:03Z",
        ):
            yield name, prescription, {**good, "at": bad}
        yield name, prescription, {k: v for k, v in good.items() if k != "at"}
        yield name, prescription, {**good, "unknown": 1}
        yield name, prescription, {**good, "hold_s" if "reps" in good else "reps": 5}
        yield name, prescription, {**good, "skipped": True}
        for bad in (False, 1, "true", None):
            yield name, prescription, {"skipped": bad, "at": AT}
        for bad in (None, [], True, "done", {}, 5):
            yield name, prescription, bad


class LoggedSetContractTests(SimpleTestCase):
    def test_valid_metric_models_and_lossless_normalizers(self):
        models = (WeightedRepsSet, BodyweightRepsSet, HoldTimeSet)
        for (name, prescription, logged), model in zip(CASES, models):
            for actuals in (
                logged,
                {**logged, "at": "2026-09-23T07:12:03.123456+00:00"},
                {"skipped": True, "at": AT},
                {k: 0 if k != "at" else AT for k in logged},
            ):
                with self.subTest(metric=prescription["type"], actuals=actuals):
                    s = {**prescription, "logged": actuals}
                    model.model_validate(s)
                    detail = detail_for(name, prescription, actuals)
                    before = copy.deepcopy(detail)
                    nd, category, _ = normalize_detail(detail, "strength")
                    validated, error = validate_detail(nd, category)
                    self.assertIsNone(error)
                    flat, error = validate_flat_detail(validated, category)
                    self.assertIsNone(error)
                    ex = flat.get("exercises", flat.get("skills"))[0]
                    self.assertEqual(ex["sets"][0], s)
                    self.assertEqual(coerce_set(s), s)
                    self.assertEqual(before, detail)
            model.model_validate(prescription)  # logged remains optional
            with self.assertRaises(ValidationError):
                model.model_validate({**prescription, "logged": None})

    def test_invalid_table_all_categories(self):
        for name, prescription, bad in invalid_cases():
            for category in ("strength", "calisthenics", "hiit", "mobility", "other"):
                with self.subTest(metric=prescription["type"], logged=bad, category=category):
                    detail = detail_for(name, prescription, bad)
                    errors = logged_detail_errors(detail)
                    self.assertTrue(errors)
                    self.assertIn("logged", errors[0]["loc"])
                    self.assertIsNotNone(validate_detail(detail, category)[1])

    def test_preservation_does_not_transfer_actuals_to_changed_or_ambiguous_sets(self):
        name, prescription, logged = CASES[0]
        stored = detail_for(name, prescription, logged)
        incoming = {"exercises": [{"name": name, "sets": [prescription]}]}
        before = copy.deepcopy(incoming)
        self.assertEqual(preserve_logged_sets(incoming, stored), stored)
        self.assertEqual(incoming, before)
        for changed in (
            {"exercises": [{"name": "Squat", "sets": [prescription]}]},
            {"exercises": [{"name": name, "sets": [{**prescription, "reps": 5}]}]},
            {"exercises": incoming["exercises"] * 2},
            {"exercises": []},
        ):
            self.assertEqual(preserve_logged_sets(changed, stored), changed)
        self.assertEqual(preserve_logged_sets(incoming, {"exercises": stored["exercises"] * 2}), incoming)
        explicit = detail_for(name, prescription, {**logged, "weight": 65})
        self.assertEqual(preserve_logged_sets(explicit, stored), explicit)
        self.assertEqual(preserve_logged_sets({"notes": "updated"}, stored)["exercises"], stored["exercises"])

    def test_summary_uses_actuals_and_distinguishes_skips_and_unlogged(self):
        name, prescription, logged = CASES[0]
        detail = {
            "exercises": [
                {
                    "name": name,
                    "sets": [
                        *[{**prescription, "logged": logged} for _ in range(3)],
                        prescription,
                        {**prescription, "logged": {"at": AT, "skipped": True}},
                        {**prescription, "logged": {**logged, "weight": 65}},
                    ],
                }
            ]
        }
        self.assertEqual(
            logged_sets_summary(detail), ["Bench press: 3×8 @ 62.5 kg logged, 1×8 @ 65 kg logged, 1 skipped"]
        )
        self.assertEqual(logged_sets_summary({"exercises": [{"name": name, "sets": [prescription]}]}), [])
        for name, prescription, logged in CASES[1:]:
            self.assertTrue(logged_sets_summary(detail_for(name, prescription, logged)))


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True, NBHD_INTERNAL_API_KEY="test-internal-key")
class LoggedSetWriteTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Logged sets", telegram_chat_id=819823)
        seed_internal_key(self.tenant)
        self.owner = APIClient()
        self.owner.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.tenant.user).access_token}")
        self.runtime = APIClient()
        self.runtime.credentials(
            HTTP_X_NBHD_INTERNAL_KEY="test-internal-key", HTTP_X_NBHD_TENANT_ID=str(self.tenant.id)
        )
        self.base = f"/api/v1/fuel/runtime/{self.tenant.id}"
        self.workout = Workout.objects.create(
            tenant=self.tenant, date=date.today(), activity="Strength", category="strength", status="planned"
        )
        self.url = f"/api/v1/fuel/workouts/{self.workout.id}/"
        congrats = patch("apps.fuel.congrats.maybe_congratulate_workout")
        congrats.start()
        self.addCleanup(congrats.stop)

    def test_patch_get_roundtrip_and_complete_same_request(self):
        for name, prescription, logged in CASES:
            for actuals in (logged, {"skipped": True, "at": AT}):
                with self.subTest(metric=prescription["type"], actuals=actuals):
                    detail = detail_for(name, prescription, actuals)
                    response = self.owner.patch(self.url, {"detail_json": detail, "status": "done"}, format="json")
                    self.assertEqual(response.status_code, 200, response.data)
                    self.assertEqual(response.data["status"], "done")
                    self.assertIsNotNone(response.data["completed_at"])
                    self.workout.refresh_from_db()
                    stamp = self.workout.completed_at
                    for data in (
                        response.data,
                        self.owner.get(self.url).data,
                        {"detail_json": self.workout.detail_json},
                    ):
                        ex = data["detail_json"].get("exercises", data["detail_json"].get("skills"))[0]
                        self.assertEqual(ex["sets"][0], {**prescription, "logged": actuals})
                    repeat = self.owner.patch(self.url, {"status": "done"}, format="json")
                    self.assertEqual(repeat.status_code, 200)
                    self.workout.refresh_from_db()
                    self.assertEqual(self.workout.completed_at, stamp)
                    reverse = self.owner.patch(self.url, {"status": "in_progress"}, format="json")
                    self.assertEqual(reverse.status_code, 200)
                    self.workout.refresh_from_db()
                    self.assertIsNone(self.workout.completed_at)
                    self.assertEqual(self.workout.detail_json, response.data["detail_json"])

    def test_invalid_table_owner_400_is_atomic(self):
        for name, prescription, bad in invalid_cases():
            with self.subTest(metric=prescription["type"], logged=bad):
                # Raw JSON deliberately exercises parser rejection of NaN/Infinity.
                response = self.owner.patch(
                    self.url,
                    json.dumps({"detail_json": detail_for(name, prescription, bad), "status": "done"}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.assertTrue("detail_json" in response.data or "JSON parse error" in str(response.data))
                self.workout.refresh_from_db()
                self.assertEqual(self.workout.detail_json, {})
                self.assertEqual(self.workout.status, "planned")
                self.assertIsNone(self.workout.completed_at)

    def test_invalid_logged_is_not_grandfathered_on_unchanged_roundtrip(self):
        name, prescription, _ = CASES[0]
        detail = detail_for(name, prescription, {"reps": -1, "weight": 5, "at": AT})
        self.workout.detail_json = detail
        self.workout.save(update_fields=["detail_json"])
        response = self.owner.patch(self.url, {"detail_json": detail}, format="json")
        self.assertEqual(response.status_code, 400, response.data)

    def test_runtime_rewrite_swaps_and_reorders_preserving_untouched_actuals(self):
        name, prescription, logged = CASES[0]
        untouched = detail_for(name, prescription, logged)["exercises"][0]
        swapped = {"name": "Squat", "sets": [{**prescription, "logged": {**logged, "weight": 90}}]}
        self.workout.detail_json = {"exercises": [untouched, swapped]}
        self.workout.save(update_fields=["detail_json"])
        replacement = {
            "exercises": [
                {"name": "Deadlift", "sets": [prescription]},
                {"name": name, "sets": [prescription]},
            ]
        }
        response = self.runtime.patch(
            f"{self.base}/workouts/{self.workout.id}/", {"detail_json": replacement}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.workout.refresh_from_db()
        exercises = self.workout.detail_json["exercises"]
        self.assertNotIn("logged", exercises[0]["sets"][0])
        self.assertEqual(exercises[1]["sets"], untouched["sets"])
        read = self.runtime.get(f"{self.base}/workouts/{self.workout.id}/")
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.data["logged_sets_summary"], ["Bench press: 1×8 @ 62.5 kg logged"])
        self.assertEqual(read.data["detail_json"]["exercises"][1]["sets"], untouched["sets"])

    def test_runtime_create_and_invalid_rewrite(self):
        name, prescription, logged = CASES[0]
        response = self.runtime.post(
            f"{self.base}/log/",
            {
                "date": str(date.today()),
                "category": "strength",
                "activity": name,
                "detail_json": detail_for(name, prescription, logged),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        workout = Workout.objects.get(pk=response.data["id"])
        self.assertEqual(workout.detail_json["exercises"][0]["sets"][0]["logged"], logged)
        bad = self.runtime.patch(
            f"{self.base}/workouts/{workout.id}/",
            {"detail_json": detail_for(name, prescription, {**logged, "weight": True})},
            format="json",
        )
        self.assertEqual(bad.status_code, 400, bad.data)
        workout.refresh_from_db()
        self.assertEqual(workout.detail_json["exercises"][0]["sets"][0]["logged"], logged)

    def test_runtime_normalizes_type_before_preserving_logged_actuals(self):
        name, prescription, logged = CASES[0]
        self.workout.detail_json = detail_for(name, prescription, logged)
        self.workout.save(update_fields=["detail_json"])
        incoming = {"exercises": [{"name": name, "sets": [{**prescription, "type": "bodyweight_reps"}]}]}
        response = self.runtime.patch(
            f"{self.base}/workouts/{self.workout.id}/", {"detail_json": incoming}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.workout.refresh_from_db()
        self.assertEqual(self.workout.detail_json["exercises"][0]["sets"][0], {**prescription, "logged": logged})

    def test_other_tenant_cannot_patch_or_read(self):
        other = create_tenant(display_name="Other lifter", telegram_chat_id=819824)
        foreign = Workout.objects.create(tenant=other, date=date.today(), activity="Private", status="planned")
        url = f"/api/v1/fuel/workouts/{foreign.id}/"
        response = self.owner.patch(url, {"status": "done", "detail_json": detail_for(*CASES[0])}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.owner.get(url).status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, "planned")
        self.assertIsNone(foreign.completed_at)
        self.assertEqual(foreign.detail_json, {})

    def test_pii_text_substitution_cannot_rewrite_logged_timestamp(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": AT}}
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["pii_entity_map", "layer1_placeholder_writes"])
        detail = detail_for(*CASES[0])
        response = self.owner.patch(self.url, {"detail_json": detail}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.workout.refresh_from_db()
        self.assertEqual(self.workout.detail_json["exercises"][0]["sets"][0]["logged"], CASES[0][2])
        runtime_url = f"{self.base}/workouts/{self.workout.id}/"
        response = self.runtime.patch(runtime_url, {"detail_json": detail}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.workout.refresh_from_db()
        self.assertEqual(self.workout.detail_json["exercises"][0]["sets"][0]["logged"], CASES[0][2])
        response = self.runtime.get(runtime_url)
        self.assertEqual(response.data["detail_json"]["exercises"][0]["sets"][0]["logged"], CASES[0][2])

    def test_plan_reconciliation_preserves_actuals_on_unchanged_sets(self):
        from apps.fuel.services import apply_reconciliation, reconcile_plan_state

        monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7)
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant, name="Strength plan", start_date=monday, weeks=1, days_per_week=1
        )
        name, prescription, logged = CASES[0]
        self.workout.plan = plan
        self.workout.date = monday
        self.workout.detail_json = detail_for(name, prescription, logged)
        self.workout.save()
        day = {
            "category": "strength",
            "activity": "Updated session",
            "detail_json": {"exercises": [{"name": name, "sets": [prescription]}]},
        }
        rec = reconcile_plan_state(plan, {"0": day}, 1, today=monday)
        apply_reconciliation(rec, plan=plan, tenant=self.tenant, writer="runtime")
        self.workout.refresh_from_db()
        self.assertEqual(self.workout.activity, "Updated session")
        self.assertEqual(self.workout.detail_json["exercises"][0]["sets"][0]["logged"], logged)

    def test_healthkit_completion_preserves_logged_sets(self):
        from apps.fuel import tests as fuel_tests

        fixture = fuel_tests.HealthKitSyncTests()
        fixture.setUp()
        workout = Workout.objects.create(
            tenant=fixture.tenant,
            date=date(2026, 6, 10),
            activity="Outdoor Run",
            category="cardio",
            status="planned",
            duration_minutes=42,
            detail_json=detail_for(*CASES[0]),
        )
        result = fixture._post({"workouts": [fixture._workout_item(external_id="hk-logged-sets")]})
        self.assertEqual(result.status_code, 200, result.data)
        workout.refresh_from_db()
        self.assertEqual(workout.status, "done")
        self.assertIsNotNone(workout.completed_at)
        self.assertEqual(workout.detail_json["exercises"][0]["sets"][0]["logged"], CASES[0][2])
