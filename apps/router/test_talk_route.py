"""Offline Talk policy, privacy, deadline and authenticated API proofs."""

import json
from threading import Event
from time import monotonic
from types import SimpleNamespace
from typing import get_args
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from pydantic import ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIClient

from apps.common import jev
from apps.common.test_jev import choice_answer, envelope
from apps.pii.redactor import RedactionOutcome
from apps.router.talk_route import (
    ACK_KINDS,
    ACK_MIN,
    QUESTIONS,
    QUICK_READ_MIN,
    QUICK_READS,
    READ_ONLY_MIN,
    REASONS,
    TALK_ROUTE_BUDGET_SECONDS,
    AckKind,
    QuickRead,
    Reason,
    TalkRouteRequest,
    TalkRouteResponse,
    decide_route,
    route_talk,
    talk_route_enabled,
)
from apps.router.talk_route_views import TalkRouteHourThrottle, TalkRouteView
from apps.tenants.models import Tenant, User


def answer(options, selected, probability=1.0, confidence=0.0):
    probabilities = {key: (1 - probability) / (len(options) - 1) for key in options}
    probabilities[selected] = probability
    return jev.ChoiceAnswer.model_validate(choice_answer(options, selected, confidence, probabilities))


def response_data():
    return envelope(
        {
            "ack_kind": answer(ACK_KINDS, "tasks").model_dump(),
            "quick_read": answer(QUICK_READS, "tasks_today").model_dump(),
            "read_only": {"type": "noul", "noul": 1.0},
        }
    )


class PolicyTests(SimpleTestCase):
    def test_single_vocabulary_and_typed_questions(self):
        schema = TalkRouteResponse.model_json_schema()
        for field, annotation, vocabulary in [
            ("ack_kind", AckKind, ACK_KINDS),
            ("quick_read", QuickRead, QUICK_READS),
            ("reason", Reason, REASONS),
        ]:
            self.assertEqual(get_args(annotation), vocabulary)
            self.assertEqual(schema["properties"][field]["enum"], list(vocabulary))
            if field in QUESTIONS:
                self.assertEqual(tuple(QUESTIONS[field].criteria), vocabulary)
        self.assertFalse(schema["additionalProperties"])
        self.assertIsInstance(QUESTIONS["read_only"], jev.NoulQuestion)
        for field in ("ack_kind", "quick_read"):
            self.assertIsInstance(QUESTIONS[field], jev.ChoiceQuestion)

    def test_thresholds_below_at_above_and_independent_ack(self):
        cases = [
            (ACK_MIN - 0.001, 1.0, 1.0, "other", "tasks_today", "low_confidence"),
            (ACK_MIN, 1.0, 1.0, "tasks", "tasks_today", "ok"),
            (ACK_MIN + 0.001, 1.0, 1.0, "tasks", "tasks_today", "ok"),
            (1.0, QUICK_READ_MIN - 0.001, 1.0, "tasks", "none", "low_confidence"),
            (1.0, QUICK_READ_MIN, 1.0, "tasks", "tasks_today", "ok"),
            (1.0, QUICK_READ_MIN + 0.001, 1.0, "tasks", "tasks_today", "ok"),
            (1.0, 1.0, READ_ONLY_MIN - 0.001, "tasks", "none", "low_confidence"),
            (1.0, 1.0, READ_ONLY_MIN, "tasks", "tasks_today", "ok"),
            (1.0, 1.0, READ_ONLY_MIN + 0.001, "tasks", "tasks_today", "ok"),
        ]
        for ack_p, quick_p, read_p, ack, quick, reason in cases:
            with self.subTest(ack_p=ack_p, quick_p=quick_p, read_p=read_p):
                result = decide_route(
                    answer(ACK_KINDS, "tasks", ack_p), answer(QUICK_READS, "tasks_today", quick_p), read_p
                )
                self.assertEqual((result.ack_kind, result.quick_read, result.reason), (ack, quick, reason))
        # Confidence is deliberately the opposite of selected probability.
        result = decide_route(answer(ACK_KINDS, "tasks", 0.59, 1.0), answer(QUICK_READS, "tasks_today", 0.84, 1.0), 1.0)
        self.assertEqual((result.ack_kind, result.quick_read), ("other", "none"))

    def test_honest_exits_and_all_quick_kinds(self):
        for selected in QUICK_READS:
            result = decide_route(answer(ACK_KINDS, "other"), answer(QUICK_READS, selected), 1.0)
            self.assertEqual((result.ack_kind, result.quick_read, result.reason), ("other", selected, "ok"))
        result = decide_route(answer(ACK_KINDS, "add_or_change"), answer(QUICK_READS, "none"), 0.0)
        self.assertEqual((result.quick_read, result.reason), ("none", "ok"))

    def test_explicit_uuid_gate(self):
        tenant = SimpleNamespace(id=uuid4())
        for raw, expected in [
            ("", False),
            ("*", False),
            ("invalid", False),
            (str(uuid4()), False),
            (f" {str(tenant.id).upper()},invalid,*", True),
        ]:
            with self.subTest(raw=raw), override_settings(TALK_ROUTE_TENANT_IDS=raw):
                self.assertEqual(talk_route_enabled(tenant), expected)
                self.assertFalse(talk_route_enabled(None))
                self.assertFalse(talk_route_enabled(SimpleNamespace(id="invalid")))

    def test_request_limits(self):
        for size in (1, 1000):
            self.assertEqual(len(TalkRouteRequest(text="あ" * size).text), size)
        for payload in ({}, {"text": ""}, {"text": "x" * 1001}, {"text": 42}, {"text": "x", "extra": True}):
            with self.subTest(payload_type=type(payload)), self.assertRaises(ValidationError):
                TalkRouteRequest.model_validate(payload)


@override_settings(OPENROUTER_API_KEY="test-only", TEST_MODE=True)
class TalkRouteViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="talk-user", email="talk@example.com")
        cls.tenant = Tenant.objects.create(user=cls.user, status=Tenant.Status.ACTIVE)

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("chat-talk-route")
        self.payload = {"text": "private-sentinel@example.com"}
        self.enterContext(override_settings(TALK_ROUTE_TENANT_IDS=str(self.tenant.id)))
        self.post = self.enterContext(
            patch("apps.common.jev._post", return_value=Mock(status_code=200, text=json.dumps(response_data())))
        )
        self.redact = self.enterContext(
            patch(
                "apps.pii.ephemeral.redact_texts_ephemeral_checked",
                return_value=[RedactionOutcome("[EMAIL_ADDRESS_1]", True, "redacted")],
            )
        )

    def send(self):
        return self.client.post(self.url, self.payload, format="json")

    def test_route_contract_and_single_parallel_call(self):
        self.assertEqual(self.url, "/api/v1/chat/talk-route/")
        with patch("apps.router.talk_route_views.perf_counter", side_effect=[10.0, 10.312]):
            response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"ack_kind": "tasks", "quick_read": "tasks_today", "reason": "ok", "latency_ms": 312}
        )
        TalkRouteResponse.model_validate_json(response.content)
        self.post.assert_called_once()
        body = self.post.call_args.kwargs["json"]
        self.assertEqual(body["state"], {"latest_message": "[EMAIL_ADDRESS_1]"})
        self.assertEqual(
            body["questions"], {key: value.model_dump(exclude_none=True) for key, value in QUESTIONS.items()}
        )
        self.assertLessEqual(self.post.call_args.kwargs["timeout"].total, TALK_ROUTE_BUDGET_SECONDS)
        self.assertEqual(self.redact.call_args.args[0], [self.payload["text"]])
        self.assertIn("deadline", self.redact.call_args.kwargs)

    def test_gate_off_and_missing_tenant_skip_work(self):
        with override_settings(TALK_ROUTE_TENANT_IDS=""):
            response = self.send()
        self.assertEqual(response.data["reason"], "disabled")
        self.assertEqual(route_talk(TalkRouteRequest(**self.payload), None).reason, "disabled")
        self.redact.assert_not_called()
        self.post.assert_not_called()

    def test_redaction_unconfirmed_or_error(self):
        for outcomes in (
            [],
            [RedactionOutcome(self.payload["text"], False, "unconfirmed")],
            [RedactionOutcome("safe", True, "ok")] * 2,
        ):
            with self.subTest(count=len(outcomes)):
                self.redact.return_value = outcomes
                self.assertEqual(self.send().data["reason"], "redaction_unconfirmed")
        self.redact.side_effect = RuntimeError(self.payload["text"])
        self.assertEqual(self.send().data["reason"], "redaction_unconfirmed")
        self.post.assert_not_called()

    def test_model_timeout_error_and_bad_schema_fail_closed(self):
        for error in (TimeoutError(self.payload["text"]), RuntimeError(self.payload["text"])):
            self.post.side_effect = error
            response = self.send()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                (response.data["reason"], response.data["ack_kind"], response.data["quick_read"]),
                ("unavailable", "other", "none"),
            )
        self.post.side_effect = None
        self.post.return_value.text = '{"private": "invalid"}'
        self.assertEqual(self.send().data["reason"], "unavailable")
        self.redact.side_effect = TimeoutError("private")
        self.assertEqual(self.send().data["reason"], "unavailable")

    def test_mixed_request_low_read_only_blocks_shortcut(self):
        self.payload["text"] = "what's on my list today and move the dentist to Friday"
        data = response_data()
        data["answers"]["ack_kind"] = answer(ACK_KINDS, "add_or_change").model_dump()
        data["answers"]["read_only"]["noul"] = 0.1
        self.post.return_value.text = json.dumps(data)
        result = self.send().data
        self.assertEqual((result["ack_kind"], result["quick_read"]), ("add_or_change", "none"))

    def test_invalid_body_and_auth(self):
        for payload in (
            {},
            {"text": ""},
            {"text": 123},
            {"text": None},
            {"text": "x" * 1001},
            [],
            {"text": "x", "extra": "private"},
        ):
            response = self.client.post(self.url, payload, format="json")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json(), {"error": "invalid_request"})
        response = self.client.post(self.url, '{"private":', content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_request"})
        self.client.force_authenticate(None)
        self.assertIn(self.send().status_code, (401, 403))
        self.post.assert_not_called()
        self.redact.assert_not_called()

    def test_throttle_wiring_and_user_scope(self):
        self.assertEqual(TalkRouteView.permission_classes, [IsAuthenticated])
        self.assertEqual(TalkRouteView.parser_classes, [JSONParser])
        self.assertEqual(TalkRouteView.throttle_classes, [TalkRouteHourThrottle])
        throttle = TalkRouteHourThrottle()
        self.assertEqual((throttle.num_requests, throttle.duration), (600, 3600))
        key = throttle.get_cache_key(SimpleNamespace(user=self.user), None)
        other_key = throttle.get_cache_key(
            SimpleNamespace(user=SimpleNamespace(pk=uuid4(), is_authenticated=True)), None
        )
        self.assertNotEqual(key, other_key)
        cache.set(key, [throttle.timer()] * 600, timeout=3600)
        self.assertEqual(self.send().status_code, 429)
        self.post.assert_not_called()

    def test_logs_never_contain_text_or_exception(self):
        for failure in (False, True):
            self.post.side_effect = RuntimeError(self.payload["text"]) if failure else None
            with self.assertLogs("apps.router.talk_route_views", level="INFO") as logs:
                self.send()
            rendered = " ".join(logs.output)
            self.assertNotIn(self.payload["text"], rendered)
            self.assertNotIn("EMAIL_ADDRESS", rendered)
            for field in ("reason=", "ack_kind=", "quick_read=", "latency_ms=", "tenant_id="):
                self.assertIn(field, rendered)

    def test_snapshot_is_detached(self):
        self.tenant.pii_entity_map = {"secret": {"nested": "original"}}
        self.tenant.pii_type_counters = {"PERSON": 2}
        self.tenant.pii_denylist = {"secret": "original"}
        route_talk(TalkRouteRequest(**self.payload), self.tenant)
        snapshot = self.redact.call_args.args[1]
        snapshot.pii_entity_map["secret"]["nested"] = "changed"
        snapshot.pii_type_counters["PERSON"] = 99
        snapshot.pii_denylist.clear()
        self.assertEqual(self.tenant.pii_entity_map["secret"]["nested"], "original")
        self.assertEqual(self.tenant.pii_type_counters, {"PERSON": 2})
        self.assertEqual(self.tenant.pii_denylist, {"secret": "original"})
        self.assertFalse(hasattr(snapshot, "save"))

    def test_deadline_bounds_stalled_redaction_and_prevents_late_jev(self):
        released, finished = Event(), Event()

        def stalled(*args, **kwargs):
            released.wait(2)
            finished.set()
            return [RedactionOutcome("safe", True, "redacted")]

        self.redact.side_effect = stalled
        started = monotonic()
        try:
            result = route_talk(TalkRouteRequest(**self.payload), self.tenant, deadline=started + 0.05)
            self.assertEqual(result.reason, "unavailable")
            self.assertLess(monotonic() - started, 0.4)
        finally:
            released.set()
            self.assertTrue(finished.wait(1))
        self.post.assert_not_called()

    def test_deadline_bounds_stalled_jev_and_saturated_pool(self):
        released, finished = Event(), Event()

        def stalled(*args, **kwargs):
            released.wait(2)
            finished.set()
            return Mock(status_code=200, text=json.dumps(response_data()))

        self.post.side_effect = stalled
        started = monotonic()
        try:
            result = route_talk(TalkRouteRequest(**self.payload), self.tenant, deadline=started + 0.05)
            self.assertEqual(result.reason, "unavailable")
            self.assertLess(monotonic() - started, 0.4)
        finally:
            released.set()
            self.assertTrue(finished.wait(1))
        with patch("apps.router.talk_route._TALK_SLOTS") as slots:
            slots.acquire.return_value = False
            self.redact.reset_mock()
            self.assertEqual(route_talk(TalkRouteRequest(**self.payload), self.tenant).reason, "unavailable")
            self.redact.assert_not_called()
            slots.release.assert_not_called()
