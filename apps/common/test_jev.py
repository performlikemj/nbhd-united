"""Offline proofs of the strict Decisions transport and content-free failures."""

import copy
import json
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings
from pydantic import ValidationError

from apps.common import jev


def envelope(answers):
    return {
        "model": jev.JEV_MODEL + "-20260917",
        "id": "test",
        "provider": "TypeSafe",
        "answers": answers,
        "usage": {"input_tokens": 100, "output_tokens": 20, "cost": 0.00001},
    }


def choice_answer(criteria, choice, confidence=1.0, probabilities=None):
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities or {key: float(key == choice) for key in criteria},
    }


def score_answer(criteria, score=2.0):
    return {
        "type": "score",
        "score": score,
        "confidence": 1.0,
        "legend": {str(i): value for i, value in enumerate(criteria)},
        "probabilities": {"0": 1 - score / 2, "1": 0.0, "2": score / 2},
    }


@override_settings(OPENROUTER_API_KEY="test-only", TEST_MODE=True)
class JevTests(SimpleTestCase):
    def setUp(self):
        self.questions = {
            "choice": jev.ChoiceQuestion(instructions="Choose", criteria={"yes": "Yes", "none": "None"}),
            "score": jev.ScoreQuestion(instructions="Help?", criteria=["None", "Some", "Much"]),
            "noul": jev.NoulQuestion(instructions="Change?"),
        }
        self.data = envelope(
            {
                "choice": choice_answer(self.questions["choice"].criteria, "yes"),
                "score": score_answer(self.questions["score"].criteria),
                "noul": {"type": "noul", "noul": 0.05},
            }
        )

    def call(self, data):
        with patch("apps.common.jev.requests.post", return_value=Mock(status_code=200, text=json.dumps(data))) as post:
            result = jev.decide({"latest_message": "[PERSON_1]"}, self.questions)
        return result, post

    def test_one_post_typed_question_schema_and_timeout(self):
        result, post = self.call(self.data)
        self.assertEqual(result.answers["score"].score, 2.0)
        self.assertEqual(result.answers["noul"].noul, 0.05)
        post.assert_called_once_with(
            jev.DECISIONS_URL,
            headers={"Authorization": "Bearer test-only"},
            json={
                "model": jev.JEV_MODEL,
                "state": {"latest_message": "[PERSON_1]"},
                "questions": {key: value.model_dump(exclude_none=True) for key, value in self.questions.items()},
            },
            timeout=(2, 4),
            allow_redirects=False,
        )

    def test_invalid_responses_fail_closed(self):
        mutations = {
            "missing": lambda d: d["answers"].pop("noul"),
            "extra answer": lambda d: d["answers"].update(extra=d["answers"]["noul"]),
            "wrong type": lambda d: d["answers"].update(choice={"type": "noul", "noul": 1.0}),
            "extra field": lambda d: d["answers"]["choice"].update(raw="private sentinel"),
            "string float": lambda d: d["answers"]["choice"].update(confidence="1.0"),
            "bool float": lambda d: d["answers"]["noul"].update(noul=True),
            "nan": lambda d: d["answers"]["choice"]["probabilities"].update(yes=float("nan")),
            "infinity": lambda d: d["answers"]["noul"].update(noul=float("inf")),
            "sum": lambda d: d["answers"]["choice"]["probabilities"].update(none=0.5),
            "wrong option": lambda d: d["answers"]["choice"].update(choice="private sentinel"),
            "not maximum": lambda d: d["answers"]["choice"].update(choice="none"),
            "missing option": lambda d: d["answers"]["choice"]["probabilities"].pop("none"),
            "extra option": lambda d: d["answers"]["choice"]["probabilities"].update(extra=0.0),
            "score mismatch": lambda d: d["answers"]["score"].update(score=0.0),
            "score sum": lambda d: d["answers"]["score"]["probabilities"].update({"0": 0.2}),
            "legend": lambda d: d["answers"]["score"]["legend"].update({"1": "wrong"}),
            "model": lambda d: d.update(model="wrong"),
            "cost": lambda d: d["usage"].update(cost=-1),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                data = copy.deepcopy(self.data)
                mutate(data)
                with self.assertRaisesMessage(jev.JevUnavailable, "Jev unavailable") as caught:
                    self.call(data)
                self.assertNotIn("private sentinel", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_bad_questions_never_reach_transport(self):
        for questions in ({}, {"x": {"type": "choice", "instructions": "x", "criteria": {"x": "x"}}}):
            with self.subTest(questions=questions), patch("apps.common.jev.requests.post") as post:
                with self.assertRaises(jev.JevUnavailable):
                    jev.decide("state", questions)
                post.assert_not_called()
        with self.assertRaises(ValidationError):
            jev.NoulQuestion(instructions=12)

    def test_failures_never_retry_or_expose_content(self):
        for failure in (requests.Timeout("private sentinel"), requests.HTTPError("private sentinel"), None):
            with self.subTest(failure=type(failure).__name__), patch("apps.common.jev.requests.post") as post:
                post.side_effect = failure
                post.return_value = Mock(status_code=200, text="private sentinel malformed JSON")
                with self.assertRaisesMessage(jev.JevUnavailable, "Jev unavailable"):
                    jev.decide("private sentinel", self.questions)
                post.assert_called_once()

    def test_redirect_and_missing_key_fail_closed(self):
        with patch("apps.common.jev.requests.post", return_value=Mock(status_code=302)) as post:
            with self.assertRaises(jev.JevUnavailable):
                jev.decide("state", self.questions)
            post.assert_called_once()
        with override_settings(OPENROUTER_API_KEY=""), patch("apps.common.jev.requests.post") as post:
            with self.assertRaises(jev.JevUnavailable):
                jev.decide("state", self.questions)
            post.assert_not_called()

    def test_test_mode_and_runner_block_unmocked_transport(self):
        # Patch beneath requests.post: if the guard regresses, fail without egress.
        for test_mode in (True, False):
            with override_settings(TEST_MODE=test_mode), patch("requests.sessions.Session.request") as network:
                with self.assertRaises(jev.JevUnavailable):
                    jev.decide("state", self.questions)
                network.assert_not_called()
