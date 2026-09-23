"""Repair must keep validated set actuals out of prose traversal."""

import copy
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from apps.fuel.authoring import logged_actuals_paths
from apps.fuel.models import Workout
from apps.fuel.set_contract import logged_detail_errors, logged_sets_summary
from apps.pii.authoring import AuthoredText
from apps.pii.repair_sweep import (
    _author_json_chunk,
    _DetectorWorkBudget,
    _json_digest,
    _json_progress,
    _partial_json_receipt,
    repair_tenant,
)
from apps.pii.store_registry import CARDIO_TRAVERSAL_VERSION, registered_store
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

AT = "2026-09-23T07:12:03Z"


def detail():
    return {
        "exercises": [
            {
                "name": "Bench press",
                "sets": [
                    {"type": "weighted_reps", "reps": 8, "weight": 60, "logged": {"reps": 8, "weight": 62.5, "at": AT}},
                    {"type": "weighted_reps", "reps": 8, "weight": 60, "logged": {"skipped": True, "at": AT}},
                ],
            }
        ],
        "skills": [
            {"name": "Push-up", "sets": [{"type": "bodyweight_reps", "reps": 10, "logged": {"reps": 12, "at": AT}}]},
            {"name": "Plank", "sets": [{"type": "hold_time", "hold_s": 30, "logged": {"hold_s": 45, "at": AT}}]},
        ],
        "notes": "Alice coached",
    }


def redact(_tenant, text, **_kwargs):
    return AuthoredText(
        text=text.replace(AT, "[PERSON_1]").replace("Alice", "[PERSON_2]"),
        receipt={"state": "placeholder", "redactions": [], "writer": "background"},
    )


class LoggedRepairTraversalTests(SimpleTestCase):
    def chunk(self, value, label="fuel.Workout", field="detail_json", *, cursor=0, limit=100):
        return _author_json_chunk(
            SimpleNamespace(layer1_placeholder_writes=True),
            value,
            paths=registered_store(label).nested_json_paths(field),
            seam="test.logged-repair",
            field=field,
            model_label=label,
            cursor=cursor,
            budget=_DetectorWorkBudget(limit),
        )

    def test_registered_workout_template_and_plan_paths_protect_entire_logged_object(self):
        original = detail()
        for label, field, value in (
            ("fuel.Workout", "detail_json", original),
            ("fuel.WorkoutTemplate", "detail_json", original),
            ("fuel.WorkoutPlan", "schedule_json", {"0": {"detail_json": original}}),
            ("fuel.WorkoutPlan", "week_overrides", {"1": {"0": {"detail_json": original}}}),
        ):
            before = copy.deepcopy(value)
            with (
                self.subTest(label=label, field=field),
                patch("apps.pii.repair_sweep.author_text", side_effect=redact) as author,
            ):
                chunk = self.chunk(value, label, field)
            self.assertTrue(chunk.complete)
            self.assertEqual(logged_actuals_paths(chunk.value), logged_actuals_paths(before))
            self.assertNotIn(AT, [call.args[1] for call in author.call_args_list])
            self.assertIn("[PERSON_2] coached", str(chunk.value))
            self.assertEqual(value, before)

    def test_invalid_logged_and_non_fuel_payloads_are_not_exempt(self):
        for logged in ({"reps": "Alice", "at": AT}, {"reps": 1, "at": AT, "notes": "Alice"}):
            with self.subTest(logged=logged), patch("apps.pii.repair_sweep.author_text", side_effect=redact):
                value = {"exercises": [{"sets": [{"reps": 1, "logged": logged}]}]}
                result = self.chunk(value).value
                self.assertNotIn(AT, str(result))
                self.assertNotIn("Alice", str(result))
        with patch("apps.pii.repair_sweep.author_text", side_effect=redact):
            chunk = self.chunk(detail(), "finance.PayoffPlan", "schedule_json")
        self.assertNotIn(AT, str(chunk.value))
        self.assertNotIn("Alice", str(chunk.value))
        with patch("apps.pii.repair_sweep.author_text", side_effect=redact):
            value = {"exercises": [{"sets": [{"type": [], "logged": {"reps": 1, "at": AT}}]}]}
            chunk = self.chunk(value)
        self.assertNotIn(AT, str(chunk.value))

    def test_logged_only_payload_uses_zero_leaf_fallback_without_corruption(self):
        value = {"exercises": [{"sets": [{"reps": 1, "logged": {"reps": 2, "at": AT}}]}]}
        with (
            patch("apps.pii.repair_sweep.author_text", side_effect=redact) as author,
            patch("apps.pii.authoring.author_text", side_effect=redact) as fallback,
        ):
            chunk = self.chunk(value)
        self.assertTrue(chunk.complete)
        self.assertEqual(chunk.value, value)
        self.assertEqual(chunk.texts_authored, 0)
        author.assert_not_called()
        self.assertEqual([call.args[1] for call in fallback.call_args_list], [""])

    def test_pre_change_cursor_restarts_and_new_chunks_resume_without_skipping_prose(self):
        value = {
            "exercises": [{"sets": [{"reps": 1, "logged": {"reps": 2, "at": AT}}]}],
            "first": "Alice first",
            "last": "Alice last",
        }
        old = {
            "reason": "repair-batch-partial",
            "repair_progress": {
                "cursor": 1,
                "source_digest": _json_digest(value),
                "traversal_version": CARDIO_TRAVERSAL_VERSION,
                "aggregate": {"state": "placeholder"},
            },
        }
        self.assertEqual(_json_progress(old, value), (0, None))
        with patch("apps.pii.repair_sweep.author_text", side_effect=redact) as author:
            first = self.chunk(value, limit=1)
            self.assertFalse(first.complete)
            self.assertEqual(first.value["first"], "[PERSON_2] first")
            self.assertEqual(first.value["last"], "Alice last")
            receipt = _partial_json_receipt({}, first.receipt, cursor=first.next_cursor, value=first.value)
            cursor, _ = _json_progress(receipt, first.value)
            second = self.chunk(first.value, cursor=cursor, limit=1)
        self.assertTrue(second.complete)
        self.assertEqual(second.value["last"], "[PERSON_2] last")
        self.assertEqual(logged_actuals_paths(second.value), logged_actuals_paths(value))
        self.assertEqual([call.args[1] for call in author.call_args_list], ["Alice first", "Alice last"])


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True, NBHD_INTERNAL_API_KEY="test-internal-key")
class LoggedRepairIntegrationTests(TestCase):
    def test_runtime_unconfirmed_actuals_survive_real_repair_and_remain_readable(self):
        tenant = create_tenant(display_name="Repair logged sets", telegram_chat_id=819825)
        tenant.layer1_placeholder_writes = True
        tenant.pii_entity_map = {"[PERSON_1]": {"name": AT}, "[PERSON_2]": {"name": "Alice"}}
        tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        seed_internal_key(tenant)
        runtime = APIClient()
        runtime.credentials(HTTP_X_NBHD_INTERNAL_KEY="test-internal-key", HTTP_X_NBHD_TENANT_ID=str(tenant.id))
        workout = Workout.objects.create(
            tenant=tenant, date=date.today(), activity="Strength", category="strength", status="in_progress"
        )
        url = f"/api/v1/fuel/runtime/{tenant.id}/workouts/{workout.id}/"
        response = runtime.patch(url, {"detail_json": detail()}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        workout.refresh_from_db()
        self.assertEqual(workout.pii_receipts["detail_json"]["state"], "unconfirmed")
        before = copy.deepcopy(workout.detail_json)
        with (
            patch("apps.pii.authoring._detect_pii", return_value=[]),
            patch("apps.pii.redactor._detect_pii", return_value=[]),
            patch("apps.pii.redactor._neural_detector_available", return_value=True),
            patch("apps.pii.authoring._residual_summary", return_value={"count": 0, "kinds": {}}),
        ):
            repaired = repair_tenant(tenant, alert=False)
            repeat = repair_tenant(tenant, alert=False)
        workout.refresh_from_db()
        self.assertEqual(repaired["fields_repaired"], 1, (repaired, repeat, workout.pii_receipts))
        self.assertEqual(repaired["errors"], 0)
        self.assertEqual(repeat["fields_attempted"], 0)
        self.assertEqual(workout.detail_json, before)
        self.assertEqual(dict(logged_actuals_paths(workout.detail_json)), dict(logged_actuals_paths(detail())))
        self.assertEqual(logged_detail_errors(workout.detail_json), [])
        self.assertEqual(logged_sets_summary(workout.detail_json), logged_sets_summary(before))
        response = runtime.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["logged_sets_summary"], logged_sets_summary(before))
        self.assertEqual(logged_actuals_paths(response.data["detail_json"]), logged_actuals_paths(before))
