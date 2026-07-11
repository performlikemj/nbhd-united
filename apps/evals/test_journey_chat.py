"""Tests for Probe 1 — chat round-trip journey canary (PR-B1).

The HTTP/poll layer is mocked; the point under test is the ASSERTION LOGIC — the
core trap being that ``replied_at`` is stamped on failures too, so a green must
require ``status==ready AND error=="" AND source==tenant`` within SLO, and a
fabricated ``source==on_device`` reply must NOT pass.
"""

from __future__ import annotations

import secrets
from unittest.mock import patch

import httpx
from django.test import TestCase, override_settings

from apps.evals.journey.chat_drive import ObservedTurn, drive_chat_turn
from apps.evals.journey.targets import JourneyConfigError
from apps.evals.models import EvalRun
from apps.evals.runner import _assert_details_safe
from apps.evals.suites.journey_chat import (
    CASE_BUDGET_CAPPED,
    CASE_ROUNDTRIP,
    SLO_MS,
    ChatOutcome,
    classify_roundtrip,
    run_chat_roundtrip_suite,
)
from apps.tenants.models import Tenant, User
from apps.tenants.pat_models import PersonalAccessToken, generate_pat


# --------------------------------------------------------------------------- #
# Fakes: a scripted httpx client + a controllable clock (no real sleeps).
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Scripted stand-in for httpx.Client. Records calls; never touches the network."""

    def __init__(self, *, post=None, post_exc=None, polls=None):
        self._post = post
        self._post_exc = post_exc
        self._polls = list(polls or [])
        self.post_calls: list[dict] = []
        self.get_calls = 0
        self.closed = False

    def post(self, url, json=None, headers=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        if self._post_exc is not None:
            raise self._post_exc
        return self._post

    def get(self, url, headers=None):
        self.get_calls += 1
        if self._polls:
            return self._polls.pop(0)
        # Exhausted script → keep returning PENDING (drives the timeout path).
        return _FakeResponse(200, _body("pending", replied=None))

    def close(self):
        self.closed = True


class _FakeClock:
    """monotonic() only advances when sleep() is called — deterministic deadlines."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, secs):
        self.t += secs


_CREATED = "2026-07-11T00:00:00+00:00"
_REPLIED_5S = "2026-07-11T00:00:05+00:00"  # 5000ms round trip
_REPLIED_60S = "2026-07-11T00:01:00+00:00"  # 60000ms — over the 45s SLO


def _body(status, *, source="tenant", error="", created=_CREATED, replied=None, waking=None, phase=""):
    return {
        "status": status,
        "source": source,
        "error": error,
        "created_at": created,
        "replied_at": replied,
        "waking_at": waking,
        "phase": phase,
        # Content keys the driver MUST ignore — planted to prove it drops them.
        "reply_text": "SECRET ASSISTANT REPLY",
        "user_text": "SECRET USER TEXT",
    }


def _observed(**kw) -> ObservedTurn:
    base = {"client_msg_id": "x", "http_ok": True, "terminal": True}
    base.update(kw)
    return ObservedTurn(**base)


# --------------------------------------------------------------------------- #
# classify_roundtrip — the assertion logic (pure, no DB/HTTP).
# --------------------------------------------------------------------------- #
class ClassifyRoundtripTest(TestCase):
    def test_ready_tenant_within_slo_is_pass(self):
        o = _observed(status="ready", source="tenant", error="", round_trip_ms=5000)
        self.assertEqual(classify_roundtrip(o), ChatOutcome.PASS)

    def test_replied_but_error_status_is_not_pass(self):
        # THE CORE TRAP: replied_at is set (round_trip_ms present, under SLO) yet
        # the turn is an error — a naive "did it reply" check would read green.
        o = _observed(status="error", error="empty_response", source="tenant", round_trip_ms=500)
        self.assertEqual(classify_roundtrip(o), ChatOutcome.PIPELINE_ERROR)
        self.assertNotEqual(classify_roundtrip(o), ChatOutcome.PASS)

    def test_on_device_source_is_not_pass(self):
        # A fabricated /turns/ reply: ready, no error, instant — passes everything
        # EXCEPT the source check.
        o = _observed(status="ready", source="on_device", error="", round_trip_ms=200)
        self.assertEqual(classify_roundtrip(o), ChatOutcome.WRONG_SOURCE)
        self.assertNotEqual(classify_roundtrip(o), ChatOutcome.PASS)

    def test_budget_exhausted_is_soft_not_pass_not_hardfail(self):
        o = _observed(status="error", error="budget_exhausted", source="tenant")
        outcome = classify_roundtrip(o)
        self.assertEqual(outcome, ChatOutcome.BUDGET_EXHAUSTED)
        self.assertNotEqual(outcome, ChatOutcome.PASS)
        self.assertNotEqual(outcome, ChatOutcome.PIPELINE_ERROR)

    def test_ready_tenant_over_slo_is_slo_breach(self):
        o = _observed(status="ready", source="tenant", error="", round_trip_ms=SLO_MS + 1)
        self.assertEqual(classify_roundtrip(o), ChatOutcome.SLO_BREACH)

    def test_never_terminal_is_timeout(self):
        o = _observed(terminal=False, timed_out=True, status="pending")
        self.assertEqual(classify_roundtrip(o), ChatOutcome.TIMEOUT)

    def test_http_failure_is_pipeline_error(self):
        o = _observed(http_ok=False, terminal=False, failure_stage="post")
        self.assertEqual(classify_roundtrip(o), ChatOutcome.PIPELINE_ERROR)

    def test_ready_without_round_trip_is_pipeline_error(self):
        o = _observed(status="ready", source="tenant", error="", round_trip_ms=None)
        self.assertEqual(classify_roundtrip(o), ChatOutcome.PIPELINE_ERROR)

    def test_ready_with_nonempty_error_is_pipeline_error(self):
        o = _observed(status="ready", source="tenant", error="weird", round_trip_ms=100)
        self.assertEqual(classify_roundtrip(o), ChatOutcome.PIPELINE_ERROR)


# --------------------------------------------------------------------------- #
# drive_chat_turn — the reusable real-path driver (mocked HTTP).
# --------------------------------------------------------------------------- #
class DriveChatTurnTest(TestCase):
    def _drive(self, client, **kw):
        clock = _FakeClock()
        with patch("apps.evals.journey.chat_drive.time", clock):
            return drive_chat_turn(
                base_url="https://cp.test",
                pat="pat_secret",
                text="ping",
                deadline_seconds=kw.pop("deadline_seconds", 90),
                poll_interval_seconds=kw.pop("poll_interval_seconds", 2),
                client=client,
                **kw,
            )

    def test_drives_and_polls_to_ready(self):
        client = _FakeClient(
            post=_FakeResponse(201, _body("pending", replied=None)),
            polls=[
                _FakeResponse(200, _body("pending", replied=None)),
                _FakeResponse(200, _body("ready", replied=_REPLIED_5S)),
            ],
        )
        o = self._drive(client)
        self.assertTrue(o.http_ok)
        self.assertTrue(o.terminal)
        self.assertEqual(o.status, "ready")
        self.assertEqual(o.source, "tenant")
        self.assertEqual(o.error, "")
        self.assertEqual(o.round_trip_ms, 5000)  # server-timestamp derived
        self.assertEqual(o.polls, 2)
        self.assertFalse(o.timed_out)
        # POST carried our client_msg_id + text + PAT bearer header.
        sent = client.post_calls[0]
        self.assertEqual(sent["json"]["client_msg_id"], o.client_msg_id)
        self.assertEqual(sent["json"]["text"], "ping")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer pat_secret")
        # An owned client is created internally; an injected one is left open.
        self.assertFalse(client.closed)

    def test_content_keys_never_leak_into_observation(self):
        # The polled JSON carries reply_text/user_text; the ObservedTurn must not.
        client = _FakeClient(
            post=_FakeResponse(201, _body("pending")),
            polls=[_FakeResponse(200, _body("ready", replied=_REPLIED_5S))],
        )
        o = self._drive(client)
        blob = repr(vars(o))
        self.assertNotIn("SECRET ASSISTANT REPLY", blob)
        self.assertNotIn("SECRET USER TEXT", blob)

    def test_immediate_budget_exhausted_on_post_skips_polling(self):
        # The no-container budget path returns status=error on the POST itself
        # (created=False → HTTP 200).
        client = _FakeClient(post=_FakeResponse(200, _body("error", error="budget_exhausted", replied=_CREATED)))
        o = self._drive(client)
        self.assertTrue(o.terminal)
        self.assertEqual(o.status, "error")
        self.assertEqual(o.error, "budget_exhausted")
        self.assertEqual(client.get_calls, 0)  # never polled — no container woken

    def test_timeout_when_never_terminal(self):
        client = _FakeClient(post=_FakeResponse(201, _body("pending")), polls=[])  # always pending
        o = self._drive(client, deadline_seconds=6, poll_interval_seconds=2)
        self.assertFalse(o.terminal)
        self.assertTrue(o.timed_out)
        self.assertGreaterEqual(o.polls, 1)

    def test_waking_at_observed_during_pending(self):
        # PR-B4 reuse: waking_at flipping non-null while PENDING is the wake signal.
        client = _FakeClient(
            post=_FakeResponse(201, _body("pending")),
            polls=[
                _FakeResponse(200, _body("pending", waking="2026-07-11T00:00:01+00:00")),
                _FakeResponse(200, _body("ready", replied=_REPLIED_5S)),
            ],
        )
        o = self._drive(client)
        self.assertTrue(o.waking_at_seen)
        self.assertEqual(o.status, "ready")

    def test_post_http_error_sets_http_ok_false(self):
        client = _FakeClient(post_exc=httpx.ConnectError("boom"))
        o = self._drive(client)
        self.assertFalse(o.http_ok)
        self.assertEqual(o.failure_stage, "post")
        self.assertFalse(o.terminal)
        self.assertEqual(client.get_calls, 0)

    def test_post_non_2xx_sets_http_ok_false(self):
        client = _FakeClient(post=_FakeResponse(500, {}))
        o = self._drive(client)
        self.assertFalse(o.http_ok)
        self.assertEqual(o.failure_stage, "post")
        self.assertEqual(o.http_status, 500)


# --------------------------------------------------------------------------- #
# run_chat_roundtrip_suite — recording + run-status wiring (drive mocked).
# --------------------------------------------------------------------------- #
def _synthetic_tenant_with_pat() -> tuple[Tenant, str]:
    email = f"{secrets.token_hex(4)}@e.com"
    user = User.objects.create_user(username=email, email=email)
    tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, is_synthetic=True)
    raw, prefix, token_hash = generate_pat()
    PersonalAccessToken.objects.create(user=user, name="eval-journey", token_prefix=prefix, token_hash=token_hash)
    return tenant, raw


class RunChatRoundtripSuiteTest(TestCase):
    def setUp(self):
        self.tenant, self.pat = _synthetic_tenant_with_pat()

    def _settings(self):
        return override_settings(
            EVAL_JOURNEY_TENANT_ID=str(self.tenant.id),
            EVAL_JOURNEY_PAT=self.pat,
            DJANGO_BASE_URL="https://cp.test",
        )

    def _run_with(self, observed: ObservedTurn) -> EvalRun:
        with self._settings(), patch("apps.evals.suites.journey_chat.drive_chat_turn", return_value=observed):
            return run_chat_roundtrip_suite(trigger=EvalRun.Trigger.MANUAL)

    def test_genuine_roundtrip_passes(self):
        run = self._run_with(_observed(status="ready", source="tenant", error="", round_trip_ms=5000))
        self.assertEqual(run.status, EvalRun.Status.PASS)
        results = list(run.results.all())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].case_id, CASE_ROUNDTRIP)
        self.assertTrue(results[0].passed)
        self.assertEqual(int(results[0].score), 5000)

    def test_pipeline_error_fails(self):
        run = self._run_with(_observed(status="error", error="empty_response", source="tenant", round_trip_ms=400))
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        r = run.results.get()
        self.assertEqual(r.case_id, CASE_ROUNDTRIP)
        self.assertFalse(r.passed)

    def test_on_device_reply_fails(self):
        run = self._run_with(_observed(status="ready", source="on_device", error="", round_trip_ms=100))
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertFalse(run.results.get().passed)

    def test_budget_exhausted_is_soft_pass_under_own_case_id(self):
        run = self._run_with(_observed(status="error", error="budget_exhausted", source="tenant"))
        # SOFT: run does not FAIL (no owner page), but it is recorded under the
        # budget case id — never as a proven round trip — with no score.
        self.assertEqual(run.status, EvalRun.Status.PASS)
        r = run.results.get()
        self.assertEqual(r.case_id, CASE_BUDGET_CAPPED)
        self.assertNotEqual(r.case_id, CASE_ROUNDTRIP)
        self.assertIsNone(r.score)

    def test_slo_breach_fails_with_score(self):
        run = self._run_with(_observed(status="ready", source="tenant", error="", round_trip_ms=SLO_MS + 5000))
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        r = run.results.get()
        self.assertFalse(r.passed)
        self.assertEqual(int(r.score), SLO_MS + 5000)
        self.assertEqual(int(r.threshold), SLO_MS)

    def test_details_are_metadata_only(self):
        run = self._run_with(_observed(status="ready", source="tenant", error="", round_trip_ms=5000, polls=2))
        details = run.results.get().details
        # record() would have raised via _assert_details_safe on anything unsafe;
        # re-assert here and check the shape is exactly codes/counts/durations.
        _assert_details_safe(details)
        self.assertEqual(details["outcome"], ChatOutcome.PASS)
        self.assertEqual(details["status"], "ready")
        self.assertEqual(details["source"], "tenant")
        self.assertEqual(details["round_trip_ms"], 5000)
        for value in details.values():
            self.assertIsInstance(value, (str, int, float, bool, type(None)))

    def test_unset_tenant_closes_error_and_raises(self):
        # Misconfigured probe = loud failure (INVARIANT #3): resolution inside
        # record_run closes the run 'error' and re-raises into the DLQ.
        with (
            override_settings(EVAL_JOURNEY_TENANT_ID="", DJANGO_BASE_URL="https://cp.test", EVAL_JOURNEY_PAT=self.pat),
            self.assertRaises(JourneyConfigError),
        ):
            run_chat_roundtrip_suite(trigger=EvalRun.Trigger.MANUAL)
        self.assertEqual(EvalRun.objects.latest("started_at").status, EvalRun.Status.ERROR)

    def test_mismatched_pat_raises(self):
        # A PAT for a DIFFERENT tenant must be refused (never drive a real account).
        _, other_pat = _synthetic_tenant_with_pat()
        with (
            override_settings(
                EVAL_JOURNEY_TENANT_ID=str(self.tenant.id),
                EVAL_JOURNEY_PAT=other_pat,
                DJANGO_BASE_URL="https://cp.test",
            ),
            self.assertRaises(JourneyConfigError),
        ):
            run_chat_roundtrip_suite(trigger=EvalRun.Trigger.MANUAL)


# --------------------------------------------------------------------------- #
# Task boundary — pass returns a summary; failure alerts owner + raises (DLQ).
# --------------------------------------------------------------------------- #
class EvalJourneyChatTaskTest(TestCase):
    def setUp(self):
        self.tenant, self.pat = _synthetic_tenant_with_pat()

    def _settings(self, **extra):
        base = {
            "EVAL_JOURNEY_TENANT_ID": str(self.tenant.id),
            "EVAL_JOURNEY_PAT": self.pat,
            "DJANGO_BASE_URL": "https://cp.test",
            "PLATFORM_OWNER_EMAIL": "owner@test.com",
        }
        base.update(extra)
        return override_settings(**base)

    def test_task_passes_returns_summary(self):
        from django.core import mail

        from apps.evals.tasks import eval_journey_chat_task

        observed = _observed(status="ready", source="tenant", error="", round_trip_ms=5000)
        with self._settings(), patch("apps.evals.suites.journey_chat.drive_chat_turn", return_value=observed):
            result = eval_journey_chat_task()
        self.assertEqual(result["status"], EvalRun.Status.PASS)
        self.assertEqual(result["suite"], "journey_chat")
        self.assertEqual(result["cases"], 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_task_failure_alerts_owner_and_raises(self):
        from django.core import mail

        from apps.evals.tasks import eval_journey_chat_task

        observed = _observed(status="error", error="empty_response", source="tenant", round_trip_ms=400)
        with (
            self._settings(),
            patch("apps.evals.suites.journey_chat.drive_chat_turn", return_value=observed),
            self.assertRaises(RuntimeError),
        ):
            eval_journey_chat_task()
        self.assertEqual(len(mail.outbox), 1)  # owner alerted before the DLQ raise


class TaskMapTest(TestCase):
    def test_eval_journey_chat_registered_zero_arg(self):
        import inspect
        from importlib import import_module

        from apps.cron.views import TASK_MAP

        self.assertIn("eval_journey_chat", TASK_MAP)
        module_path, func_name = TASK_MAP["eval_journey_chat"].rsplit(".", 1)
        func = getattr(import_module(module_path), func_name)
        self.assertTrue(callable(func))
        inspect.signature(func).bind()  # zero-arg no-body-publish contract
