"""Structured lesson contracts and deterministic variety retries."""

import json
from typing import get_args
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, override_settings
from pydantic import ValidationError

from apps.core import compose
from apps.core.lesson import LESSON_JSON_SCHEMA, TRADITIONS, MeditationLesson, Tradition
from apps.core.tests import _valid_manifest


def answer(slug="wu-wei", tradition="taoist"):
    manifest = _valid_manifest()
    manifest["lesson"].update(teaching_slug=slug, tradition=tradition)
    return ({"choices": [{"message": {"content": json.dumps(manifest)}}]}, "test/model")


@override_settings(OPENROUTER_API_KEY="test-key", CORE_COMPOSE_MODEL="test/primary")
class LessonTests(SimpleTestCase):
    def setUp(self):
        cache = patch.object(compose, "_SCHEMA_REJECTED_MODELS", set())
        cache.start()
        self.addCleanup(cache.stop)
        self.signals = {
            "recent_meditations": [
                {
                    "date": "2026-09-07",
                    "title": "Release",
                    "lesson": {"tradition": "taoist", "teaching_slug": "Wu_Wei", "core_teaching": "Release force."},
                },
                {
                    "date": "2026-09-06",
                    "title": "Attention",
                    "lesson": {
                        "tradition": "zen",
                        "teaching_slug": "beginners-mind",
                        "core_teaching": "Meet this moment.",
                    },
                },
            ]
        }

    def test_vocabulary_schema_and_validation(self):
        self.assertEqual(get_args(Tradition), TRADITIONS)
        self.assertEqual(LESSON_JSON_SCHEMA["properties"]["tradition"]["enum"], list(TRADITIONS))
        self.assertIn(", ".join(TRADITIONS), compose._SYSTEM_PROMPT)
        lesson = _valid_manifest()["lesson"]
        lesson["teaching_slug"] = "  Wu_WEI! "
        self.assertEqual(MeditationLesson.model_validate(lesson).teaching_slug, "wu-wei")
        for field, value in (
            ("tradition", "invented"),
            ("teaching_slug", "x" * 41),
            ("core_teaching", "x" * 201),
            ("summary", "x" * 401),
            ("practice", "x" * 201),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                MeditationLesson.model_validate({**lesson, field: value})

    @patch("apps.core.compose.chat_completion")
    def test_clash_retry_contains_slug_then_accepts(self, completion):
        completion.side_effect = [answer(tradition="buddhist"), answer("dichotomy-of-control", "stoic")]
        with self.assertLogs("apps.core.compose", level="INFO") as logs:
            manifest = compose.author_manifest(self.signals)
        self.assertEqual(manifest["lesson"]["tradition"], "stoic")
        calls = completion.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[0], calls[1].args[0])
        prompt = calls[1].args[1][-1]["content"]
        self.assertIn("wu-wei", prompt)
        self.assertIn("2026-09-07", prompt)
        self.assertIn("zen", prompt)
        self.assertIn("accepted_after_retry", " ".join(logs.output))
        schema = calls[0].kwargs["response_format"]
        self.assertEqual(schema["type"], "json_schema")
        self.assertIn("lesson", schema["json_schema"]["schema"]["required"])

    @patch("apps.core.compose.chat_completion")
    def test_two_clashes_move_to_next_model(self, completion):
        completion.side_effect = [
            answer(),
            answer("different-teaching", "zen"),
            answer("attention-rest", "science_of_mind"),
        ]
        result = compose.author_manifest(self.signals)
        self.assertEqual(result["lesson"]["teaching_slug"], "attention-rest")
        self.assertNotEqual(completion.call_args_list[1].args[0], completion.call_args_list[2].args[0])
        self.assertEqual(len(completion.call_args_list[2].args[1]), 2)

    @patch("apps.core.compose.chat_completion", side_effect=lambda *a, **kw: answer())
    def test_all_clash_accepts_with_warning_or_strict_error(self, completion):
        with self.assertLogs("apps.core.compose", level="WARNING") as logs:
            result = compose.author_manifest(self.signals)
        self.assertEqual(result["lesson"]["teaching_slug"], "wu-wei")
        self.assertIn("compose: variety clash accepted", " ".join(logs.output))
        self.assertIn("outcome=clash_accepted", " ".join(logs.output))
        self.assertEqual(completion.call_count, 2 * len(compose._compose_models()))
        with (
            self.settings(CORE_COMPOSE_STRICT_VARIETY=True),
            self.assertRaisesRegex(compose.ComposeError, "variety_clash"),
        ):
            compose.author_manifest(self.signals)

    @patch("apps.core.compose.chat_completion")
    def test_valid_clashes_then_transport_failure_accepts_last_valid_sit(self, completion):
        completion.side_effect = [answer(), answer(), RuntimeError("unavailable")]
        with (
            patch("apps.core.compose._compose_models", return_value=["first", "second"]),
            self.assertLogs("apps.core.compose", level="WARNING") as logs,
        ):
            result = compose.author_manifest(self.signals)
        self.assertEqual(result["lesson"]["teaching_slug"], "wu-wei")
        self.assertIn("clash_accepted", " ".join(logs.output))

    @patch("apps.core.compose.chat_completion")
    def test_schema_4xx_fallback_is_per_model(self, completion):
        response = requests.Response()
        response.status_code = 400
        completion.side_effect = [requests.HTTPError(response=response), answer(), answer("open-awareness", "buddhist")]
        result = compose.author_manifest(self.signals)
        self.assertEqual(result["lesson"]["tradition"], "buddhist")
        self.assertEqual(
            [c.kwargs["response_format"]["type"] for c in completion.call_args_list],
            ["json_schema", "json_object", "json_object"],
        )

    @patch("apps.core.compose.chat_completion")
    def test_schema_rejection_is_cached_across_composes_and_logged_once(self, completion):
        response = requests.Response()
        response.status_code = 400
        completion.side_effect = [requests.HTTPError(response=response), answer(), answer(), answer()]
        with self.assertLogs("apps.core.compose", level="INFO") as logs:
            compose.author_manifest({}, model="first")
            compose.author_manifest({}, model="first")
            compose.author_manifest({}, model="second")
        self.assertEqual(
            [c.kwargs["response_format"]["type"] for c in completion.call_args_list],
            ["json_schema", "json_object", "json_object", "json_schema"],
        )
        self.assertEqual(sum("schema rejected" in line for line in logs.output), 1)

    @patch("apps.core.compose.chat_completion")
    def test_provider_failure_detail_is_bounded(self, completion):
        response = requests.Response()
        response.status_code = 403
        completion.side_effect = requests.HTTPError("provider detail " + "x" * 100, response=response)
        with self.assertRaises(compose.ComposeError) as caught:
            compose.author_manifest({}, model="first")
        self.assertIn("HTTPError status=403: " + ("provider detail " + "x" * 100)[:80], str(caught.exception))
        self.assertNotIn("x" * 81, str(caught.exception))

    @patch("apps.core.compose.chat_completion")
    def test_invalid_lesson_skips_model_and_does_not_log_private_input(self, completion):
        private = answer(tradition="PRIVATE_SENTINEL")
        completion.side_effect = [private, answer("open-awareness", "buddhist")]
        with self.assertLogs("apps.core.compose", level="WARNING") as logs:
            result = compose.author_manifest({})
        self.assertEqual(result["lesson"]["tradition"], "buddhist")
        self.assertNotIn("PRIVATE_SENTINEL", " ".join(logs.output))
        self.assertNotEqual(completion.call_args_list[0].args[0], completion.call_args_list[1].args[0])

    def test_lookback_lines_are_whole_and_include_lesson(self):
        entries = self.signals["recent_meditations"]
        full = compose._recent_meditation_lines(entries)
        self.assertIn("[taoist/Wu_Wei: Release force.]", full[0])
        self.assertEqual(compose._recent_meditation_lines(entries, budget=len(full[0]) + 1), full[:1])
        self.assertEqual(compose._RECENT_MEDITATIONS_CHAR_BUDGET, 2600)

    @patch("apps.core.compose.chat_completion")
    def test_all_clash_fallback_keeps_last_structurally_valid_sit(self, completion):
        invalid = _valid_manifest()
        invalid["phases"] = []
        bad_answer = ({"choices": [{"message": {"content": json.dumps(invalid)}}]}, "test/model")
        completion.side_effect = [answer(), bad_answer]
        with self.assertLogs("apps.core.compose", level="WARNING"):
            result = compose.author_manifest(self.signals, model="test/model")
        self.assertTrue(result["phases"])
        completion.side_effect = [bad_answer, bad_answer]
        with self.assertRaises(compose.ComposeError):
            compose.author_manifest(self.signals, model="test/model")
