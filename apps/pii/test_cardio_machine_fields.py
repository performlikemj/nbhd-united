"""Cardio machine leaves survive every PII traversal; future prose does not."""

import copy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.fuel.test_cardio_segments import EXAMPLES
from apps.pii.store_registry import registered_store, rewrite_json_path


def detail():
    value = copy.deepcopy(EXAMPLES["intervals_mixed"])
    value["segments"][1]["target_pace"] = "6:00"
    value["segments"][1]["notes"] = "Run with Alice"
    value["notes"] = "Alice is joining"
    value["planned"] = {"duration_s": 2700, "future_machine_value": "easy"}
    return value


def payloads(value):
    return [
        ("fuel.Workout", "detail_json", value),
        ("fuel.WorkoutTemplate", "detail_json", value),
        ("fuel.WorkoutPlan", "schedule_json", {"0": {"detail_json": value}}),
        ("fuel.WorkoutPlan", "week_overrides", {"1": {"0": {"detail_json": value}}}),
    ]


class CardioPathTests(SimpleTestCase):
    def test_registry_exclusions_cover_repair_and_future_text(self):
        for label, field, original in payloads(detail()):
            with self.subTest(label=label, field=field):
                store = registered_store(label)
                result = original
                for path in store.nested_json_paths(field):
                    result, _ = rewrite_json_path(
                        result,
                        path,
                        lambda text: text.replace("Alice", "[PERSON_1]").upper(),
                        exclude_paths=store.nested_json_exclusions(field),
                    )
                expected = copy.deepcopy(original)
                target = (
                    expected
                    if field == "detail_json"
                    else expected["0"]["detail_json"]
                    if field == "schedule_json"
                    else expected["1"]["0"]["detail_json"]
                )
                target["notes"] = "[PERSON_1] IS JOINING"
                target["segments"][1]["notes"] = "RUN WITH [PERSON_1]"
                self.assertEqual(result, expected)

    def test_tool_response_redaction_and_annotation_keep_machine_leaves(self):
        from apps.pii.redactor import _TOOL_SKIP_KEYS, _annotate_model_value, _redact_tool_value

        original = {
            "workout": {"detail_json": detail()},
            "schedule_json": {"0": {"detail_json": detail()}},
            "week_overrides": {"1": {"0": {"detail_json": detail()}}},
        }
        with patch("apps.pii.redactor.redact_user_message", side_effect=lambda text, *a, **k: text.upper()):
            redacted = _redact_tool_value(original, SimpleNamespace(), {}, _TOOL_SKIP_KEYS)
        with patch("apps.pii.redactor.annotate_model_context", side_effect=lambda text, *a: text.upper()):
            annotated = _annotate_model_value(redacted, {}, _TOOL_SKIP_KEYS)
        for value in (
            annotated["workout"]["detail_json"],
            annotated["schedule_json"]["0"]["detail_json"],
            annotated["week_overrides"]["1"]["0"]["detail_json"],
        ):
            self.assertEqual(value["segments"][1]["target_pace"], "6:00")
            self.assertEqual(value["segments"][1]["effort"], "hard")
            self.assertEqual(value["segments"][1]["recovery"]["effort"], "easy")
            self.assertEqual(value["terrain"], "track")
            self.assertEqual(value["planned"], original["workout"]["detail_json"]["planned"])
            self.assertEqual(value["notes"], "ALICE IS JOINING")
            self.assertEqual(value["segments"][1]["notes"], "RUN WITH ALICE")


class CardioAuthoringTests(TestCase):
    def test_owner_and_runtime_writes_keep_machine_fields_and_author_notes(self):
        from apps.pii.store_authoring import author_store_fields
        from apps.tenants.services import create_tenant

        tenant = create_tenant(display_name="Cardio author", telegram_chat_id=812911)
        tenant.layer1_placeholder_writes = True
        tenant.pii_entity_map = {
            "[PERSON_1]": {"name": "Alice"},
            "[PERSON_2]": {"name": "easy"},
            "[PERSON_3]": {"name": "track"},
        }
        tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        for writer in ("owner", "runtime"):
            for label, field, original in payloads(detail()):
                with self.subTest(writer=writer, label=label, field=field):
                    result, _ = author_store_fields(
                        tenant,
                        {field: original},
                        model_label=label,
                        seam="test.cardio",
                        writer=writer,
                        defer_detection=True,
                    )
                    expected = copy.deepcopy(original)
                    target = (
                        expected
                        if field == "detail_json"
                        else expected["0"]["detail_json"]
                        if field == "schedule_json"
                        else expected["1"]["0"]["detail_json"]
                    )
                    target["notes"] = "[PERSON_1] is joining"
                    target["segments"][1]["notes"] = "Run with [PERSON_1]"
                    self.assertEqual(result[field], expected)
