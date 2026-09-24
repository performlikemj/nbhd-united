"""Policy, parser, privacy and authenticated endpoint proofs; no model network."""

import json
import re
from copy import deepcopy
from datetime import UTC, date, datetime
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from pydantic import ValidationError
from rest_framework.test import APIClient, APIRequestFactory

from apps.common import jev
from apps.common.test_jev import choice_answer, envelope, score_answer
from apps.pii.ephemeral import redact_texts_ephemeral_checked, warm_ephemeral_path
from apps.pii.redactor import RedactionOutcome
from apps.router.chat_shape import (
    PANELS,
    QUESTIONS,
    SHAPE_BUDGET_SECONDS,
    SURFACES,
    ChatShapeRequest,
    ChatShapeResponse,
    OpenPanel,
    chat_shape_enabled,
    chat_shape_panels,
    decide_panel,
    parse_date,
    parse_duration,
    truncate_redacted,
)
from apps.router.chat_shape_views import ChatShapeHourThrottle
from apps.router.panels import LOG_METRICS, PANEL_KINDS
from apps.tenants.models import Tenant, User


def response_data(surface="sleep", time_range="this_week", follow_up=0.03, score=2.0):
    return envelope(
        {
            "surface": choice_answer(SURFACES, surface),
            "visual_helps": score_answer(QUESTIONS["visual_helps"]["criteria"], score),
            "follow_up_on_open_panel": {"type": "noul", "noul": follow_up},
            "time_range": choice_answer(QUESTIONS["time_range"]["criteria"], time_range),
            "wants_to_change": {"type": "noul", "noul": 0.06},
        }
    )


class PolicyTests(SimpleTestCase):
    def test_policy_table(self):
        # message, surface, helps, follow, confidence, range, open, disabled, expected
        cases = [
            ("sleep", "sleep", 2, 0, 1, "this_week", None, False, ("open", "sleep", "ok")),
            ("calendar", "schedule", 2, 0, 1, "tomorrow", None, False, ("open", "schedule", "ok")),
            ("training", "training_week", 2, 0, 1, "this_week", None, False, ("open", "training_week", "ok")),
            ("workout", "workout_detail", 2, 0, 1, "today", None, False, ("open", "workout", "ok")),
            ("weight", "body_weight", 2, 0, 1, "this_month", None, False, ("open", "log_table", "ok")),
            ("journal", "journal", 2, 0, 1, "this_week", None, False, ("open", "journal_table", "ok")),
            ("focus", "timer", 2, 0, 1, "today", None, False, ("open", "timer", "ok")),
            ("i feel kind of down", "none", 0, 0, 1, "today", None, False, ("none", None, "no_panel")),
            ("packing list", "checklist", 2, 0, 1, "today", None, False, ("none", None, "no_panel")),
            ("not helpful", "sleep", 0.74, 0.49, 1, "today", None, False, ("none", None, "no_panel")),
            ("help boundary", "sleep", 0.75, 0, 0.5, "today", None, False, ("open", "sleep", "ok")),
            ("low confidence", "sleep", 2, 0, 0.49, "today", None, False, ("none", None, "low_confidence")),
            ("last week?", "sleep", 0, 0.5, 1, "last_week", "sleep", False, ("update", "sleep", "ok")),
            ("today?", "workout_detail", 2, 1, 1, "today", "training_week", False, ("update", "workout", "ok")),
            ("this week?", "training_week", 2, 1, 1, "this_week", "workout", False, ("update", "training_week", "ok")),
            ("new topic", "timer", 2, 0, 1, "today", "sleep", False, ("open", "timer", "ok")),
            ("different family", "timer", 2, 1, 1, "today", "sleep", False, ("open", "timer", "ok")),
            ("same no follow", "sleep", 2, 0.49, 1, "today", "sleep", False, ("open", "sleep", "ok")),
            ("disabled", "sleep", 2, 1, 1, "today", "sleep", True, ("none", None, "panel_disabled")),
            ("no_panel before low", "none", 2, 1, 0.1, "today", "sleep", True, ("none", None, "no_panel")),
        ]
        for message, surface, helps, follow, confidence, period, opened, disabled, expected in cases:
            with self.subTest(message=message):
                result = decide_panel(
                    jev.ChoiceAnswer.model_validate(choice_answer(SURFACES, surface, confidence)),
                    helps,
                    follow,
                    period,
                    OpenPanel(kind=opened, label="label") if opened else None,
                    frozenset() if disabled else frozenset(PANELS),
                )
                self.assertEqual(result, expected)
        for surface in set(SURFACES) - {
            "sleep",
            "schedule",
            "training_week",
            "workout_detail",
            "timer",
            "body_weight",
            "journal",
        }:
            with self.subTest(unsupported=surface):
                answer = jev.ChoiceAnswer.model_validate(choice_answer(SURFACES, surface))
                self.assertEqual(decide_panel(answer, 2, 1, "today"), ("none", None, "no_panel"))

    def test_workout_tie_boundaries(self):
        for top, second, confidence, period, expected in [
            (0.53, 0.44, 0.53, "today", ("open", "workout", "ok")),
            (0.48, 0.44, 0.48, "today", ("open", "workout", "ok")),
            (0.575, 0.425, 0.575, "today", ("open", "workout", "ok")),
            (0.58, 0.42, 0.58, "today", ("open", "training_week", "ok")),
            (0.53, 0.44, 0.53, "this_week", ("open", "training_week", "ok")),
            (0.48, 0.44, 0.48, "this_week", ("open", "training_week", "ok")),
        ]:
            with self.subTest(message="what's today's workout", top=top, period=period):
                probs = {key: 0.0 for key in SURFACES}
                probs.update(training_week=top, workout_detail=second, none=max(0, 1 - top - second))
                answer = jev.ChoiceAnswer.model_validate(choice_answer(SURFACES, "training_week", confidence, probs))
                self.assertEqual(decide_panel(answer, 2, 0, period), expected)
        probs = {key: 0.0 for key in SURFACES}
        probs.update(sleep=0.4, training_week=0.31, workout_detail=0.29)
        answer = jev.ChoiceAnswer.model_validate(choice_answer(SURFACES, "sleep", 0.4, probs))
        self.assertEqual(decide_panel(answer, 2, 0, "today"), ("none", None, "low_confidence"))

    def test_fitness_family_table(self):
        # Unspecified residual mass is outside the family. No live Jev calls.
        cases = [
            ("how's my fitness", "training_week", 0.33, 0.39, 0.0, 0.37, "this_week", "training_week"),
            ("am i getting fitter", "body_weight", 0.72, 0.1, 0.0, 0.72, "unspecified", "log_table"),
            ("how are my workouts going", "training_week", 0.99, 0.99, 0.0, 0.0, "this_week", "training_week"),
            ("family boundary", "training_week", 0.3, 0.3, 0.1, 0.2, "this_week", "training_week"),
            ("below family boundary", "training_week", 0.3, 0.3, 0.1, 0.199, "this_week", None),
            ("weak weight winner", "body_weight", 0.3, 0.3, 0.0, 0.4, "unspecified", "training_week"),
            ("weight alone boundary", "body_weight", 0.3, 0.0, 0.0, 0.6, "today", "log_table"),
            ("below weight boundary", "body_weight", 0.3, 0.001, 0.0, 0.599, "today", "training_week"),
            ("workout tie today", "workout_detail", 0.3, 0.35, 0.4, 0.1, "today", "workout"),
            ("workout tie week", "workout_detail", 0.3, 0.35, 0.4, 0.1, "this_week", "training_week"),
            ("confident workout", "workout_detail", 0.99, 0.0, 0.99, 0.0, "today", "workout"),
            ("unhelpful family", "training_week", 0.33, 0.39, 0.0, 0.37, "this_week", None),
        ]
        for label, top, confidence, training, workout, weight, period, panel in cases:
            with self.subTest(label=label):
                probs = dict.fromkeys(SURFACES, 0.0)
                probs.update(training_week=training, workout_detail=workout, body_weight=weight)
                residual = max(0, 1 - sum(probs.values()))
                for key in ("none", "sleep", "nutrition"):
                    probs[key] = residual / 3
                answer = jev.ChoiceAnswer.model_validate(choice_answer(SURFACES, top, confidence, probs))
                helps = 0 if label == "unhelpful family" else 2
                expected = (
                    ("open", panel, "ok") if panel else ("none", None, "no_panel" if helps == 0 else "low_confidence")
                )
                self.assertEqual(decide_panel(answer, helps, 0, period), expected)
                if panel:
                    self.assertEqual(
                        decide_panel(answer, helps, 0, period, enabled_panels=frozenset()),
                        ("none", None, "panel_disabled"),
                    )

    def test_shared_vocabulary_metric_scope_and_default_panels(self):
        self.assertEqual(set(PANELS), set(PANEL_KINDS) - {"tasks"})
        self.assertEqual(chat_shape_panels(), frozenset(PANELS))
        for metric in LOG_METRICS:
            self.assertEqual(
                ChatShapeResponse(decision="open", panel="log_table", metric=metric, reason="ok").metric, metric
            )
            for panel in set(PANELS) - {"log_table"}:
                with self.subTest(metric=metric, panel=panel), self.assertRaises(ValidationError):
                    ChatShapeResponse(decision="open", panel=panel, metric=metric, reason="ok")
        for data in (
            {"metric": "body_weight"},
            {"decision": "open", "panel": "log_table", "metric": "other", "reason": "ok"},
        ):
            with self.assertRaises(ValidationError):
                ChatShapeResponse(**data)
        self.assertEqual(SHAPE_BUDGET_SECONDS, 4.0)


class ParserTests(SimpleTestCase):
    def test_date_table(self):
        tenant = SimpleNamespace(user=SimpleNamespace(timezone="Asia/Tokyo"))
        now = datetime(2026, 9, 23, 16, tzinfo=UTC)  # Thursday in Tokyo; Wednesday UTC.
        cases = [
            ("today", "today", "2026-09-24"),
            ("what's today's workout", "today", "2026-09-24"),
            ("yesterday", "yesterday", "2026-09-23"),
            ("tomorrow", "tomorrow", "2026-09-25"),
            ("this week", "this_week", None),
            ("last week", "last_week", None),
            ("this month", "this_month", None),
            ("what about last month?", "last_month", None),
            ("and friday?", "unspecified", "2026-09-25"),
            ("next friday", "unspecified", "2026-09-25"),
            ("on friday", "unspecified", "2026-09-25"),
            ("last friday", "unspecified", "2026-09-18"),
            ("this monday", "unspecified", "2026-09-21"),
            ("last thursday", "unspecified", "2026-09-17"),
            ("this thursday", "unspecified", "2026-09-24"),
            ("this sunday", "unspecified", "2026-09-27"),
            ("thursday", "unspecified", "2026-09-24"),
            ("next thursday", "unspecified", "2026-10-01"),
            ("MONDAY", "unspecified", "2026-09-28"),
            ("tuesday", "unspecified", "2026-09-29"),
            ("wednesday", "unspecified", "2026-09-30"),
            ("saturday", "unspecified", "2026-09-26"),
            ("sunday", "unspecified", "2026-09-27"),
            ("past 1 day", "today", None),
            ("past 4 days", "today", None),
            ("past 5 days", "this_week", None),
            ("past 14 days", "this_week", None),
            ("past 19 days", "this_month", None),
            ("past 90 days", "this_month", None),
            ("something else", "unspecified", None),
        ]
        for text, period, day in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_date(text, tenant, now=now), (period, date.fromisoformat(day) if day else None))
        tenant.user.timezone = "bad-zone"
        self.assertEqual(parse_date("today", tenant, now=now), ("today", date(2026, 9, 23)))
        tenant.user.timezone = "America/Los_Angeles"
        self.assertEqual(
            parse_date("tomorrow", tenant, now=datetime(2026, 9, 24, 1, tzinfo=UTC)), ("tomorrow", date(2026, 9, 24))
        )

    def test_duration_table(self):
        for text, expected in [
            ("25 min", 1500),
            ("25 minute focus", 1500),
            ("1 hour", 3600),
            ("an hour", 3600),
            ("90 seconds", 90),
            ("1.5 hours", 5400),
            ("1 hour 30 minutes", 5400),
            ("1 hour and 30 minutes", 5400),
            ("1h30", 5400),
            ("1h30m", 5400),
            ("1,500 seconds", 1500),
            ("90 sec", 90),
            ("1,50 seconds", None),
            ("1 500 seconds", None),
            ("1 hour 1,50 minutes", None),
            ("1 hour.", 3600),
            ("1h30.", 5400),
            ("1h 30", 5400),
            ("90 sec, please", 90),
            ("1/2 hour", None),
            ("1h99", None),
            ("1 hour 30 minutes 15 seconds", 5415),
            ("2 hrs", 7200),
            ("a minute", 60),
            ("30s", 30),
            ("pause timer", None),
            ("0 minutes", None),
            ("-5 min", None),
        ]:
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), expected)

    def test_gate_exact_ids_and_exact_wildcard(self):
        tenant = SimpleNamespace(id=uuid4())
        for value, expected in [
            ("", False),
            ("*", True),
            ("**", False),
            ("*x", False),
            (str(uuid4()), False),
            (f" {str(tenant.id).upper()} , other", True),
        ]:
            with self.subTest(value=value), override_settings(CHAT_SHAPE_TENANT_IDS=value):
                self.assertEqual(chat_shape_enabled(tenant), expected)
                self.assertFalse(chat_shape_enabled(None))


@override_settings(OPENROUTER_API_KEY="test-only", TEST_MODE=True, CHAT_SHAPE_PANELS=",".join(PANELS))
class ChatShapeViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="shape-user", email="shape@example.com", timezone="Asia/Tokyo")
        cls.tenant = Tenant.objects.create(user=cls.user, status=Tenant.Status.ACTIVE)

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("chat-shape")
        self.payload = {
            "client_msg_id": "00000000-0000-4000-8000-000000000001",
            "text": "how did i sleep this week",
            "open_panel": None,
            "recent_turns": [],
        }
        self.gate = override_settings(CHAT_SHAPE_TENANT_IDS=str(self.tenant.id))
        self.gate.enable()
        self.addCleanup(self.gate.disable)
        self.post = self.enterContext(patch("apps.common.jev._post"))
        self.post.return_value = Mock(status_code=200, text=json.dumps(response_data()))
        self.warm = self.enterContext(patch("apps.pii.ephemeral.warm_ephemeral_path", return_value=True))
        self.redact = self.enterContext(patch("apps.pii.ephemeral.redact_texts_ephemeral_checked"))
        self.redact.side_effect = lambda texts, tenant, **kwargs: [
            RedactionOutcome(text, True, "redacted") for text in texts
        ]

    def send(self):
        return self.client.post(self.url, self.payload, format="json")

    def test_contract_example_and_route(self):
        self.assertEqual(self.url, "/api/v1/chat/shape/")
        with patch("apps.router.chat_shape_views.perf_counter", side_effect=[10.0, 10.312]):
            response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "enabled": True,
                "decision": "open",
                "panel": "sleep",
                "metric": None,
                "range": "this_week",
                "day": None,
                "duration_seconds": None,
                "wants_change": 0.06,
                "follow_up": 0.03,
                "confidence": 1.0,
                "reason": "ok",
                "latency_ms": 312,
            },
        )
        ChatShapeResponse.model_validate_json(response.content)
        self.post.assert_called_once()

    def test_gate_off_skips_redactor_and_jev(self):
        with override_settings(CHAT_SHAPE_TENANT_IDS=""):
            response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["reason"], "disabled")
        self.assertFalse(response.data["enabled"])
        self.post.assert_not_called()
        self.redact.assert_not_called()
        self.warm.assert_not_called()

    def test_all_text_redacted_and_history_truncated(self):
        self.payload.update(
            text="private newest",
            recent_turns=[{"role": "user", "text": "x" * 600}, {"role": "assistant", "text": "private reply"}],
            open_panel={"kind": "sleep", "label": "private label"},
        )
        self.redact.side_effect = None
        self.redact.return_value = [
            RedactionOutcome(text, True, "redacted")
            for text in ("safe newest", "safe user", "safe reply", "safe label")
        ]
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(
            self.post.call_args.kwargs["json"]["state"],
            {
                "latest_message": "safe newest",
                "recent_turns": ["user: safe user", "assistant: safe reply"],
                "open_panel": "sleep: safe label",
            },
        )
        self.redact.assert_called_once()
        self.assertEqual(self.redact.call_args.args[0], ["private newest", "x" * 600, "private reply", "private label"])
        for call in self.redact.call_args_list:
            self.assertIn("deadline", call.kwargs)
            self.assertEqual(call.args[1].id, self.tenant.id)

    def test_fresh_topic_omits_history_from_redaction_and_jev(self):
        self.payload["recent_turns"] = [{"role": "user", "text": "PRIVATE_HISTORY_SENTINEL"}]
        self.assertEqual(self.send().data["reason"], "ok")
        self.redact.assert_called_once()
        self.assertEqual(self.redact.call_args.args[0], [self.payload["text"]])
        state = self.post.call_args.kwargs["json"]["state"]
        self.assertNotIn("recent_turns", state)
        self.assertNotIn("PRIVATE_HISTORY_SENTINEL", str(state))

    def test_table_panels_metric_and_followups(self):
        for surface, panel, metric in (("body_weight", "log_table", "body_weight"), ("journal", "journal_table", None)):
            for opened, follow, decision in ((None, 0, "open"), (panel, 1, "update"), ("training_week", 1, "open")):
                with self.subTest(surface=surface, opened=opened):
                    self.payload.update(
                        text="show this month", open_panel={"kind": opened, "label": "Table"} if opened else None
                    )
                    self.post.return_value.text = json.dumps(response_data(surface, "this_month", follow))
                    result = self.send().data
                    self.assertEqual((result["decision"], result["panel"], result["metric"]), (decision, panel, metric))
                    with override_settings(CHAT_SHAPE_PANELS="sleep"):
                        result = self.send().data
                    self.assertEqual(
                        (result["reason"], result["panel"], result["metric"]), ("panel_disabled", None, None)
                    )

    def test_any_unconfirmed_text_skips_jev(self):
        self.payload.update(
            recent_turns=[
                {"role": "user", "text": "first"},
                {"role": "assistant", "text": "second"},
                {"role": "user", "text": "third"},
                {"role": "assistant", "text": "fourth"},
            ],
            open_panel={"kind": "sleep", "label": "label"},
        )
        for position in range(6):
            with self.subTest(position=position):
                self.redact.side_effect = None
                self.redact.return_value = [RedactionOutcome("text", i != position, "redacted") for i in range(6)]
                response = self.send()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["reason"], "redaction_unconfirmed")
                self.assertEqual(response.data["decision"], "none")
                self.post.assert_not_called()

    def test_deterministic_date_overrides_jev_and_weekday_does_not_fall_back(self):
        self.post.return_value.text = json.dumps(response_data(time_range="this_month"))
        for text, expected_range, expected_day in [
            ("what about last month?", "last_month", None),
            ("and friday?", "unspecified", "2026-09-25"),
            ("tomorrow", "tomorrow", "2026-09-25"),
        ]:
            self.payload["text"] = text
            with (
                self.subTest(text=text),
                patch("apps.router.chat_shape.timezone.now", return_value=datetime(2026, 9, 23, 16, tzinfo=UTC)),
            ):
                response = self.send()
            self.assertEqual(response.data["range"], expected_range)
            self.assertEqual(response.data["day"], expected_day)
        for jev_range, expected in [("this_week", "this_week"), ("longer", "unspecified")]:
            self.payload["text"] = "show sleep"
            self.post.return_value.text = json.dumps(response_data(time_range=jev_range))
            self.assertEqual(self.send().data["range"], expected)

    def test_policy_and_duration_through_endpoint(self):
        for text, surface, period, opened, follow, decision, panel, duration in [
            ("i feel kind of down", "none", "unspecified", None, 0, "none", None, None),
            ("packing list", "checklist", "unspecified", None, 0, "none", None, None),
            ("25 minute focus", "timer", "today", "sleep", 0, "open", "timer", 1500),
            ("pause timer", "timer", "today", "timer", 1, "update", "timer", None),
            ("today", "workout_detail", "today", "training_week", 1, "update", "workout", None),
            ("slept 90 seconds", "sleep", "today", None, 0, "open", "sleep", None),
        ]:
            with self.subTest(text=text):
                self.payload.update(text=text, open_panel={"kind": opened, "label": "label"} if opened else None)
                self.post.return_value.text = json.dumps(response_data(surface, period, follow))
                response = self.send()
                self.assertEqual(
                    (response.data["decision"], response.data["panel"], response.data["duration_seconds"]),
                    (decision, panel, duration),
                )
        with override_settings(CHAT_SHAPE_PANELS="timer"):
            self.assertEqual(self.send().data["reason"], "panel_disabled")

    def test_bad_requests_are_content_free_400(self):
        cases = [
            {"text": "x" * 2001},
            {"text": 123},
            {"client_msg_id": "bad"},
            {"recent_turns": [{"role": "user", "text": "x"}] * 5},
            {"recent_turns": [{"role": "system", "text": "x"}]},
            {"open_panel": {"kind": "checklist", "label": "x"}},
            {"extra": "private sentinel"},
            {"recent_turns": [{"role": "user", "text": 5}]},
            {"recent_turns": [{"role": "user", "text": "x" * 4001}]},
            {"open_panel": {"kind": "sleep", "label": "x" * 4001}},
        ]
        original = self.payload.copy()
        for changes in cases:
            with self.subTest(changes=list(changes)):
                self.payload = original | changes
                response = self.send()
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data, {"error": "invalid_request"})
        for data in ('{"private sentinel":', '["private sentinel"]', "{}"):
            response = self.client.post(self.url, data=data, content_type="application/json")
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("private sentinel", response.content.decode())
        self.post.assert_not_called()

    def test_auth_required(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.send().status_code, 401)
        self.post.assert_not_called()

    def test_throttle_is_user_scoped_300_hour(self):
        throttle = ChatShapeHourThrottle()
        self.assertEqual((throttle.num_requests, throttle.duration), (300, 3600))
        factory = APIRequestFactory()
        first, second = factory.post(self.url), factory.post(self.url)
        first.user = self.user
        second.user = SimpleNamespace(pk=uuid4(), is_authenticated=True)
        self.assertNotEqual(throttle.get_cache_key(first, None), throttle.get_cache_key(second, None))
        cache.set(throttle.get_cache_key(first, None), [throttle.timer()] * 300, 3600)
        self.assertEqual(self.send().status_code, 429)
        self.post.assert_not_called()

    def test_sentinel_absent_from_logs_and_error_responses(self):
        sentinel = "PRIVATE_SHAPE_SENTINEL_8675309"
        self.payload["text"] = sentinel
        for failure in ("transport", "provider", "redaction", "request"):
            with self.subTest(failure=failure):
                self.post.side_effect = RuntimeError(sentinel) if failure == "transport" else None
                self.post.return_value = Mock(status_code=200, text=sentinel)
                self.redact.side_effect = (
                    RuntimeError(sentinel)
                    if failure == "redaction"
                    else lambda texts, *args, **kwargs: [
                        RedactionOutcome("[PERSON_1]", True, "redacted") for _ in texts
                    ]
                )
                self.payload["text"] = sentinel * 100 if failure == "request" else sentinel
                with self.assertLogs(level="INFO") as captured:
                    response = self.send()
                self.assertNotIn(sentinel, response.content.decode())
                self.assertNotIn(sentinel, "\n".join(captured.output))
                if failure != "request":
                    expected = "redaction_unconfirmed" if failure == "redaction" else "unavailable"
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.data["reason"], expected)
                    if expected == "unavailable":
                        self.assertTrue(any(record.levelname == "WARNING" for record in captured.records))

    def test_request_boundary_accepts_2000_and_four_turns(self):
        payload = self.payload | {
            "text": "x" * 2000,
            "recent_turns": [{"role": "user", "text": "x" * 4000}] * 4,
            "open_panel": {"kind": "sleep", "label": "x" * 4000},
        }
        parsed = ChatShapeRequest.model_validate(payload)
        self.assertEqual(len(parsed.recent_turns), 4)
        self.assertTrue(all(len(turn.text) == 4000 for turn in parsed.recent_turns))
        self.assertEqual(len(parsed.open_panel.label), 4000)

    def test_history_uses_only_last_two_at_300_chars(self):
        self.payload["open_panel"] = {"kind": "sleep", "label": "Sleep"}
        self.payload["recent_turns"] = [
            {"role": "user", "text": "unused one"},
            {"role": "assistant", "text": "unused two"},
            {"role": "user", "text": "a" * 500},
            {"role": "assistant", "text": "b" * 500},
        ]
        self.assertEqual(self.send().status_code, 200)
        self.redact.assert_called_once()
        self.assertEqual(
            self.redact.call_args.args[0],
            [self.payload["text"], "unused one", "unused two", "a" * 500, "b" * 500, "Sleep"],
        )
        state = self.post.call_args.kwargs["json"]["state"]
        self.assertEqual(state["recent_turns"], ["user: " + "a" * 300, "assistant: " + "b" * 300])

    def test_real_redaction_never_writes_even_if_jev_fails_and_chat_stays_provisional(self):
        from apps.pii.provisional import PiiIngress
        from apps.pii.redactor import redact_user_message_checked

        self.tenant.pii_entity_map = {"[PERSON_4]": {"name": "Knownfixture"}}
        self.tenant.pii_type_counters = {"PERSON": 9}
        self.tenant.save(update_fields=["pii_entity_map", "pii_type_counters"])
        self.user.tenant = self.tenant
        before_map = deepcopy(self.tenant.pii_entity_map)
        before_counters = deepcopy(self.tenant.pii_type_counters)
        self.redact.side_effect = redact_texts_ephemeral_checked
        self.payload.update(
            text="Knownfixture wants sleep",
            recent_turns=[{"role": "assistant", "text": "Fakenamealpha suggested it"}],
            open_panel={"kind": "sleep", "label": "Fakenamebeta sleep"},
        )
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=fake_name_detector),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
        ):
            for fails in (False, True):
                self.tenant.user = self.user
                with (
                    self.subTest(jev_fails=fails),
                    patch.object(Tenant, "save", side_effect=AssertionError("No writes")),
                    patch.object(Tenant.objects, "select_for_update", side_effect=AssertionError("No registry lock")),
                    self.assertNumQueries(0),
                ):
                    self.post.side_effect = RuntimeError("unavailable") if fails else None
                    response = self.send()
                    self.assertEqual(response.data["reason"], "unavailable" if fails else "ok")
                    state = self.post.call_args.kwargs["json"]["state"]
                    self.assertNotIn("Fakename", str(state))
                    self.assertIn("[PERSON_4]", state["latest_message"])
                    self.assertIn("[PERSON_10]", state["recent_turns"][0])
                self.tenant.refresh_from_db()
                self.assertEqual(self.tenant.pii_entity_map, before_map)
                self.assertEqual(self.tenant.pii_type_counters, before_counters)
            with override_settings(PII_PROVISIONAL_TENANT_IDS=frozenset({str(self.tenant.pk)})):
                outcome = redact_user_message_checked(
                    "Fakenamealpha",
                    self.tenant,
                    allow_user_name=False,
                    ingress=PiiIngress("ios", "shape-following-chat", datetime(2026, 9, 24, tzinfo=UTC)),
                )
            self.assertTrue(outcome.confirmed)
            self.tenant.refresh_from_db()
            self.assertEqual(outcome.text, "[PERSON_10]")
            self.assertTrue(self.tenant.pii_entity_map["[PERSON_10]"]["provisional"])

    def test_cold_shared_client_is_reused_after_deadline(self):
        from apps.pii.shared_client import SharedPiiPipeline

        class ColdClient(SharedPiiPipeline):
            def _call(self, text, *, deadline=None):
                cold = not getattr(self, "warmed", False)
                self.warmed = True
                sleep(0.2 if cold else 0.01)
                return []

        pipeline = ColdClient()
        self.redact.side_effect = redact_texts_ephemeral_checked
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=pipeline),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
            # Capture fresh clients too: recreating one means every call is cold.
            patch.object(SharedPiiPipeline, "_call", ColdClient._call),
            patch("apps.router.chat_shape_views.SHAPE_BUDGET_SECONDS", 0.1),
        ):
            self.assertEqual(self.send().data["reason"], "unavailable")
            self.post.assert_not_called()
            for _ in range(3):
                started = monotonic()
                self.assertEqual(self.send().data["reason"], "ok")
                self.assertLess(monotonic() - started, 0.1)
            # Let the abandoned first invocation exit before removing patches.
            sleep(0.15)
        self.assertEqual(self.post.call_count, 3)

    def test_background_warmup_outlives_deadlines_without_occupying_shape_slots(self):
        from apps.pii import ephemeral
        from apps.pii.shared_client import SharedPiiPipeline

        entered, release = Event(), Event()
        clients = []

        class ColdClient(SharedPiiPipeline):
            def _call(self, text, *, deadline=None):
                clients.append(self)
                if text == "Warmup.":
                    entered.set()
                    release.wait(5)
                return fake_name_detector(text)

        pipeline = ColdClient()
        warmup = ephemeral._EphemeralWarmup()
        self.warm.side_effect = warm_ephemeral_path
        self.redact.side_effect = redact_texts_ephemeral_checked
        self.payload["text"] = "Fakenamealpha asks how did i sleep this week"
        with (
            patch.object(ephemeral, "_ephemeral_warmup", warmup),
            patch("apps.pii.engine.get_pii_pipeline", return_value=pipeline),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
            patch("apps.router.chat_shape_views.SHAPE_BUDGET_SECONDS", 0.05),
        ):
            try:
                with patch("apps.router.chat_shape._SHAPE_WORKERS.submit") as submit:
                    # More expired calls than pool slots still schedule one warmup.
                    for _ in range(6):
                        self.assertEqual(self.send().data["reason"], "unavailable")
                    self.assertTrue(entered.is_set())
                    submit.assert_not_called()
                    self.post.assert_not_called()
                    self.assertEqual(clients, [pipeline])
            finally:
                release.set()
                self.assertTrue(warmup._done.wait(1))
            for _ in range(3):
                started = monotonic()
                self.assertEqual(self.send().data["reason"], "ok")
                self.assertLess(monotonic() - started, 0.05)
            self.assertEqual(clients, [pipeline] * 4)
            self.assertEqual(self.post.call_count, 3)
            self.assertNotIn("Fakenamealpha", str(self.post.call_args))

    def test_failed_warmup_never_confirms_request_or_prevents_recovery(self):
        from apps.pii import ephemeral
        from apps.pii.shared_client import SharedPiiPipeline

        warmup = ephemeral._EphemeralWarmup()
        self.warm.side_effect = warm_ephemeral_path
        self.redact.side_effect = redact_texts_ephemeral_checked
        pipeline = SharedPiiPipeline()
        with (
            patch.object(ephemeral, "_ephemeral_warmup", warmup),
            patch("apps.pii.engine.get_pii_pipeline", return_value=pipeline),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
            patch.object(pipeline, "_call", side_effect=RuntimeError("PRIVATE_SENTINEL")) as call,
        ):
            self.assertEqual(self.send().data["reason"], "redaction_unconfirmed")
            self.post.assert_not_called()
            call.side_effect = None
            call.return_value = []
            self.assertEqual(self.send().data["reason"], "ok")
            self.post.assert_called_once()

    def test_timing_log_once_per_call_including_early_returns(self):
        from apps.router.chat_shape import shape_chat

        parsed = ChatShapeRequest.model_validate(self.payload)
        cases = ["ok", "redaction_unconfirmed", "unavailable", "disabled"]
        for reason in cases:
            with self.subTest(reason=reason):
                self.redact.side_effect = (
                    RuntimeError("PRIVATE_SENTINEL")
                    if reason == "redaction_unconfirmed"
                    else lambda texts, *args, **kwargs: [RedactionOutcome("safe", True, "redacted") for _ in texts]
                )
                with (
                    override_settings(CHAT_SHAPE_TENANT_IDS="" if reason == "disabled" else str(self.tenant.id)),
                    self.assertLogs("apps.router.chat_shape", level="INFO") as captured,
                ):
                    result = shape_chat(parsed, self.tenant, deadline=0 if reason == "unavailable" else None)
                self.assertEqual(result.reason, reason)
                self.assertEqual(len(captured.records), 1)
                record = captured.records[0]
                self.assertEqual(record.reason, reason)
                self.assertGreaterEqual(record.redact_ms, 0)
                self.assertGreaterEqual(record.jev_ms, 0)
                if reason != "ok":
                    self.assertEqual(record.jev_ms, 0)
                self.assertNotIn("PRIVATE_SENTINEL", captured.output[0])
                self.assertIn("redact_ms=", captured.output[0])
                self.assertIn("jev_ms=", captured.output[0])
                expected_texts = 0 if reason in {"disabled", "unavailable"} else 1
                self.assertEqual(record.texts, expected_texts)
                self.assertIn(f"texts={expected_texts}", captured.output[0])

    def test_maximum_history_batches_slow_detector_and_shares_jev_budget(self):
        from apps.pii.redactor import _detect_pii

        self.redact.side_effect = redact_texts_ephemeral_checked
        self.payload["recent_turns"] = [{"role": "user", "text": "word " * 100}] * 4
        self.payload["open_panel"] = {"kind": "sleep", "label": "Sleep"}

        def slow_detector(text):
            sleep(0.1)
            return []

        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=slow_detector),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
            patch(
                "apps.pii.redactor._detect_pii",
                wraps=_detect_pii,
            ) as detect,
        ):
            started = monotonic()
            response = self.send()
            self.assertLess(monotonic() - started, SHAPE_BUDGET_SECONDS)
            self.assertEqual(response.data["reason"], "ok")
            self.assertGreater(detect.call_count, 1)
            self.assertLessEqual(self.post.call_args.kwargs["timeout"].total, SHAPE_BUDGET_SECONDS - 0.2)
            # The detector covers complete history, not its eventual snippets.
            self.assertTrue(all(len(call.args[0].encode("utf-8")) <= 384 for call in detect.call_args_list))

    def test_slow_detector_returns_at_deadline_without_late_jev(self):

        entered, release, finished = Event(), Event(), Event()

        def blocked_detector(text):
            entered.set()
            release.wait(5)
            return []

        def redact(*args, **kwargs):
            try:
                return redact_texts_ephemeral_checked(*args, **kwargs)
            finally:
                finished.set()

        self.redact.side_effect = redact
        self.payload["recent_turns"] = [{"role": "user", "text": "word " * 100}] * 4
        self.payload["open_panel"] = {"kind": "sleep", "label": "Sleep"}
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=blocked_detector),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
        ):
            started = monotonic()
            try:
                response = self.send()
                elapsed = monotonic() - started
                self.assertTrue(entered.is_set())
                self.assertEqual(response.data["reason"], "unavailable")
                self.assertGreaterEqual(elapsed, SHAPE_BUDGET_SECONDS - 0.1)
                self.assertLess(elapsed, SHAPE_BUDGET_SECONDS + 0.4)
                self.post.assert_not_called()
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
            self.post.assert_not_called()

    def test_slow_jev_cannot_extend_endpoint_deadline(self):
        entered, release, finished = Event(), Event(), Event()

        def blocked_post(*args, **kwargs):
            entered.set()
            try:
                release.wait(2)
                return Mock(status_code=200, text=json.dumps(response_data()))
            finally:
                finished.set()

        self.post.side_effect = blocked_post
        with patch("apps.router.chat_shape_views.SHAPE_BUDGET_SECONDS", 0.15):
            started = monotonic()
            try:
                response = self.send()
                self.assertTrue(entered.is_set())
                self.assertEqual(response.data["reason"], "unavailable")
                self.assertLess(monotonic() - started, 0.6)
            finally:
                release.set()
                self.assertTrue(finished.wait(1))

    def test_registered_email_crossing_old_cuts_reaches_jev_as_whole_placeholder(self):
        email = "alice.private@example.com"
        placeholder = "[EMAIL_ADDRESS_1]"
        long_name = "Verylongfirstname Verylongmiddlename Verylongfamilyname"
        self.tenant.pii_entity_map = {placeholder: email, "[PERSON_1]": long_name}
        self.user.tenant = self.tenant
        self.redact.side_effect = redact_texts_ephemeral_checked
        # Deliberately miss every neural/pattern span. Only the registered
        # complete value can protect the email fragment from the review probe.
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=lambda text: []),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
        ):
            for boundary in (300, 500):
                prefix = (long_name + " ") * 5 if boundary == 500 else ""
                padding = boundary - 11 - len(prefix)
                prefix += "safe " * (padding // 5) + " " * (padding % 5)
                complete = prefix + email + " trailing context"
                self.assertTrue(complete[:boundary].endswith("alice.priva"))
                for field in ("user", "assistant", "label", "latest"):
                    with self.subTest(boundary=boundary, field=field):
                        self.payload.update(text="sleep", recent_turns=[], open_panel=None)
                        if field in {"user", "assistant"}:
                            self.payload["recent_turns"] = [{"role": field, "text": complete}]
                            self.payload["open_panel"] = {"kind": "sleep", "label": "Sleep"}
                        elif field == "label":
                            self.payload["open_panel"] = {"kind": "sleep", "label": complete}
                        else:
                            self.payload["text"] = complete
                        response = self.send()
                        self.assertEqual(response.data["reason"], "ok")
                        state = self.post.call_args.kwargs["json"]["state"]
                        value = (
                            state["recent_turns"][0]
                            if field in {"user", "assistant"}
                            else state["open_panel"]
                            if field == "label"
                            else state["latest_message"]
                        )
                        self.assertIn(placeholder, value)
                        self.assertNotIn("alice", json.dumps(state))
                        self.assertNotIn(email, json.dumps(state))


class RedactedTruncationTests(SimpleTestCase):
    def test_boundary_keeps_complete_placeholders(self):
        cases = [
            ("plain text", 5, "plain"),
            ("before [EMAIL_ADDRESS_1] after", 12, "before [EMAIL_ADDRESS_1]"),
            ("before [PERSON_123|friend] after", 19, "before [PERSON_123|friend]"),
            ("before [PERSON_1]", 7, "before "),
            ("[PERSON_1] after", 10, "[PERSON_1]"),
            ("before [PERSON_1] then [LOCATION_2] after", 29, "before [PERSON_1] then [LOCATION_2]"),
            ("日本語 [PERSON_1] 次", 8, "日本語 [PERSON_1]"),
        ]
        for text, limit, expected in cases:
            with self.subTest(text=text, limit=limit):
                self.assertEqual(truncate_redacted(text, limit), expected)


def fake_name_detector(text):
    return [
        {"entity_group": "FIRSTNAME", "start": match.start(), "end": match.end(), "score": 0.99}
        for match in re.finditer(r"Fakenamealpha|Fakenamebeta", text)
    ]
