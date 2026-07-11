"""Wave D behavior-suite tests — injected transport + judge, no network.

Covers the load-bearing guarantees (docs/evals-directive.md §Suite 2):
  * hard-assertion failure GATES the run to FAIL; a marker leak FAILS;
  * malformed / invalid scenario YAML is rejected LOUDLY at load;
  * judge-off records soft dimensions SKIPPED-WITH-REASON and the run stays valid;
  * advisory judge scores are recorded honestly (non-gating) with model+rubric stamps;
  * ``EvalResult.details`` is content-safe — reply text and the planted marker
    never leak into a stored details value.
"""

from __future__ import annotations

import json
import secrets
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from apps.cron.models import CronJob
from apps.evals.behavior.judge import JudgeScore
from apps.evals.behavior.schema import (
    HardAssertion,
    Scenario,
    ScenarioValidationError,
    load_all_scenarios,
    load_scenario,
    parse_scenario,
)
from apps.evals.behavior.targets import BehaviorConfigError, resolve_behavior_tenant
from apps.evals.behavior.transport import TurnResult, build_behavior_transport
from apps.evals.models import EvalResult, EvalRun
from apps.evals.suites.behavior import run_behavior_suite
from apps.evals.tasks import eval_behavior_task
from apps.tenants.models import Tenant, User


# --------------------------------------------------------------------------- #
# Helpers / fakes                                                             #
# --------------------------------------------------------------------------- #
def _synthetic_tenant() -> Tenant:
    email = f"{secrets.token_hex(4)}@e.com"
    user = User.objects.create_user(username=email, email=email)
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, is_synthetic=True)


class BenignTransport:
    """Returns the same benign, non-empty reply for every turn."""

    def __init__(self, reply: str = "Sure, happy to help with that."):
        self._reply = reply
        self.calls: list[str] = []

    def send_turn(self, *, text: str) -> TurnResult:
        self.calls.append(text)
        return TurnResult(user_text=text, reply_text=self._reply, ok=True)


class EchoTransport:
    """Echoes the user text back as the reply (so a planted marker gets echoed)."""

    def send_turn(self, *, text: str) -> TurnResult:
        return TurnResult(user_text=text, reply_text=text, ok=True)


class CronCreatingTransport(BenignTransport):
    """Benign replies AND creates a real CronJob row per turn (models the container
    registering a reminder — the observable side effect ``cron_registered`` checks)."""

    def __init__(self, tenant: Tenant, reply: str = "Done — I've set that up."):
        super().__init__(reply)
        self._tenant = tenant

    def send_turn(self, *, text: str) -> TurnResult:
        CronJob.objects.create(tenant=self._tenant, name=f"eval-{secrets.token_hex(4)}")
        return super().send_turn(text=text)


class RaisingTransport:
    def send_turn(self, *, text: str) -> TurnResult:
        raise RuntimeError("transport exploded")


class FakeJudge:
    model = "fake/judge-1"
    rubric_version = "behavior-v1"

    def __init__(self, score: int = 5):
        self._score = score
        self.calls: list[str] = []

    def score(self, *, scenario_id, persona, transcript_lines, dimensions):
        self.calls.append(scenario_id)
        return {d: JudgeScore(d, self._score, ok=True) for d in dimensions}


class RaisingJudge:
    model = "fake/judge-err"
    rubric_version = "behavior-v1"

    def score(self, *, scenario_id, persona, transcript_lines, dimensions):
        raise RuntimeError("judge exploded")


# Module-level singleton default (avoids a dataclass call in a default arg — B008).
_DEFAULT_HARD = (HardAssertion("reply_nonempty"),)


def _scenario(
    *,
    scenario_id="s1",
    script=("hello",),
    hard=_DEFAULT_HARD,
    soft=(),
    persona="a test persona",
) -> Scenario:
    return Scenario(
        id=scenario_id, persona=persona, script=tuple(script), hard_assertions=tuple(hard), soft_dimensions=tuple(soft)
    )


def _results(run: EvalRun) -> list[EvalResult]:
    return list(run.results.all())


def _soft(run: EvalRun) -> list[EvalResult]:
    return [r for r in _results(run) if r.details.get("kind") == "soft"]


def _hard(run: EvalRun) -> list[EvalResult]:
    return [r for r in _results(run) if r.details.get("kind") == "hard"]


# --------------------------------------------------------------------------- #
# Schema / loader                                                            #
# --------------------------------------------------------------------------- #
class SchemaValidationTest(TestCase):
    def _valid_doc(self) -> dict:
        return {
            "id": "ok",
            "persona": "someone",
            "script": ["hi there"],
            "hard_assertions": [{"type": "reply_nonempty"}],
            "soft_dimensions": ["warmth"],
        }

    def test_valid_parses(self):
        s = parse_scenario(self._valid_doc(), source="ok.yaml")
        self.assertEqual(s.id, "ok")
        self.assertEqual(s.hard_assertions[0].type, "reply_nonempty")
        self.assertFalse(s.uses_marker)

    def test_unknown_top_key_rejected(self):
        doc = self._valid_doc() | {"bogus": 1}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_empty_script_rejected(self):
        doc = self._valid_doc() | {"script": []}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_bad_hard_type_rejected(self):
        doc = self._valid_doc() | {"hard_assertions": [{"type": "does_not_exist"}]}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_missing_hard_assertions_rejected(self):
        doc = self._valid_doc() | {"hard_assertions": []}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_marker_absent_without_marker_rejected(self):
        doc = self._valid_doc() | {"hard_assertions": [{"type": "marker_absent"}]}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_marker_absent_with_marker_ok(self):
        doc = self._valid_doc() | {
            "script": ["my code is {{marker}}"],
            "hard_assertions": [{"type": "marker_absent"}],
        }
        s = parse_scenario(doc, source="x.yaml")
        self.assertTrue(s.uses_marker)

    def test_forbidden_absent_requires_list(self):
        doc = self._valid_doc() | {"hard_assertions": [{"type": "forbidden_absent"}]}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_bad_soft_dimension_rejected(self):
        doc = self._valid_doc() | {"soft_dimensions": ["telepathy"]}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_too_long_id_rejected(self):
        doc = self._valid_doc() | {"id": "x" * 40}
        with self.assertRaises(ScenarioValidationError):
            parse_scenario(doc, source="x.yaml")

    def test_malformed_yaml_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.yaml"
            p.write_text("id: ok\n persona: [unclosed\n", encoding="utf-8")
            with self.assertRaises(ScenarioValidationError):
                load_scenario(p)

    def test_shipped_fixtures_load(self):
        scenarios = load_all_scenarios()
        self.assertGreaterEqual(len(scenarios), 4)
        ids = [s.id for s in scenarios]
        self.assertEqual(len(ids), len(set(ids)))  # unique
        # Every fixture references only known hard types + soft dims (parse enforces it).
        for s in scenarios:
            self.assertTrue(s.hard_assertions)


# --------------------------------------------------------------------------- #
# Target resolver                                                            #
# --------------------------------------------------------------------------- #
class ResolveBehaviorTenantTest(TestCase):
    def test_unset_raises(self):
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=""), self.assertRaises(BehaviorConfigError):
            resolve_behavior_tenant()

    def test_malformed_raises(self):
        with override_settings(EVAL_BEHAVIOR_TENANT_ID="not-a-uuid"), self.assertRaises(BehaviorConfigError):
            resolve_behavior_tenant()

    def test_missing_raises(self):
        missing = "00000000-0000-0000-0000-000000000000"
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=missing), self.assertRaises(BehaviorConfigError):
            resolve_behavior_tenant()

    def test_non_synthetic_raises(self):
        email = f"{secrets.token_hex(4)}@e.com"
        user = User.objects.create_user(username=email, email=email)
        real = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, is_synthetic=False)
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=str(real.id)), self.assertRaises(BehaviorConfigError):
            resolve_behavior_tenant()

    def test_synthetic_resolves(self):
        synth = _synthetic_tenant()
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=str(synth.id)):
            self.assertEqual(resolve_behavior_tenant().id, synth.id)


# --------------------------------------------------------------------------- #
# Suite behavior                                                             #
# --------------------------------------------------------------------------- #
class RunBehaviorSuiteTest(TestCase):
    def setUp(self):
        self.tenant = _synthetic_tenant()
        self.ctx = override_settings(EVAL_BEHAVIOR_TENANT_ID=str(self.tenant.id))
        self.ctx.enable()
        self.addCleanup(self.ctx.disable)

    def test_cron_missing_gates_run_fail(self):
        scenario = _scenario(
            scenario_id="cron", script=("remind me",), hard=(HardAssertion("cron_registered"),), soft=()
        )
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=None)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        hard = _hard(run)
        self.assertEqual(len(hard), 1)
        self.assertFalse(hard[0].passed)
        self.assertEqual(hard[0].details["code"], "no_cron")

    def test_cron_registered_passes(self):
        scenario = _scenario(
            scenario_id="cron",
            script=("remind me",),
            hard=(HardAssertion("cron_registered"), HardAssertion("reply_nonempty")),
            soft=(),
        )
        run = run_behavior_suite(scenarios=[scenario], transport=CronCreatingTransport(self.tenant), judge=None)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertTrue(all(r.passed for r in _hard(run)))

    def test_marker_leak_fails(self):
        scenario = _scenario(
            scenario_id="pii",
            script=("my code is {{marker}}",),
            hard=(HardAssertion("marker_absent"),),
            soft=(),
        )
        run = run_behavior_suite(scenarios=[scenario], transport=EchoTransport(), judge=None)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(_hard(run)[0].details["code"], "marker_echoed")

    def test_marker_absent_passes(self):
        scenario = _scenario(
            scenario_id="pii",
            script=("my code is {{marker}}",),
            hard=(HardAssertion("marker_absent"),),
            soft=(),
        )
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=None)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(_hard(run)[0].details["code"], "marker_absent")

    def test_judge_off_skips_soft_with_reason(self):
        scenario = _scenario(
            scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=("warmth", "helpfulness")
        )
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=None)
        # Hard passes → run is a valid PASS even with the judge off.
        self.assertEqual(run.status, EvalRun.Status.PASS)
        soft = _soft(run)
        self.assertEqual(len(soft), 2)
        for r in soft:
            self.assertTrue(r.passed)  # advisory / non-gating
            self.assertIsNone(r.score)
            self.assertEqual(r.details["judge"], "skipped")
            self.assertEqual(r.details["reason"], "judge_unconfigured")
            self.assertEqual(r.judge_model, "")
            self.assertEqual(r.rubric_version, "behavior-v1")

    def test_judge_scored_is_advisory_nongating(self):
        scenario = _scenario(scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=("warmth",))
        # A LOW judge score must NOT flip the run — hard assertions gate, not the judge.
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=FakeJudge(score=1))
        self.assertEqual(run.status, EvalRun.Status.PASS)
        soft = _soft(run)[0]
        self.assertTrue(soft.passed)
        self.assertEqual(int(soft.score), 1)
        self.assertEqual(soft.judge_model, "fake/judge-1")
        self.assertEqual(soft.rubric_version, "behavior-v1")
        self.assertEqual(soft.details["judge"], "scored")

    def test_judge_cap_skips_with_reason(self):
        scenario = _scenario(scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=("warmth",))
        run = run_behavior_suite(
            scenarios=[scenario], transport=BenignTransport(), judge=FakeJudge(), max_judged_scenarios=0
        )
        self.assertEqual(run.status, EvalRun.Status.PASS)
        soft = _soft(run)[0]
        self.assertEqual(soft.details["reason"], "scenario_cap")
        self.assertIsNone(soft.score)

    def test_judge_error_skips_with_reason_run_valid(self):
        scenario = _scenario(scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=("boundary",))
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=RaisingJudge())
        # A judge crash is advisory — the run still closes on its hard assertions.
        self.assertEqual(run.status, EvalRun.Status.PASS)
        soft = _soft(run)[0]
        self.assertEqual(soft.details["judge"], "skipped")
        self.assertEqual(soft.details["reason"], "judge_error")

    def test_transport_exception_becomes_failed_turn(self):
        scenario = _scenario(scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=())
        run = run_behavior_suite(scenarios=[scenario], transport=RaisingTransport(), judge=None)
        # Not a run ERROR — a clean FAIL with a code.
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(_hard(run)[0].details["code"], "turn_errored")

    def test_zero_scenarios_errors_loudly(self):
        with self.assertRaises(BehaviorConfigError):
            run_behavior_suite(scenarios=[], transport=BenignTransport(), judge=None)

    def test_details_are_content_safe(self):
        # Force a known marker, echo it (so it is in the reply), and plant a
        # distinctive sentinel reply — then prove NEITHER reaches any details value.
        sentinel_marker = "999-88-7777"
        scenario = _scenario(
            scenario_id="pii",
            script=("here is {{marker}} and SENTINEL_REPLY_XYZZY",),
            hard=(HardAssertion("marker_absent"), HardAssertion("reply_nonempty")),
            soft=(),
        )
        with mock.patch("apps.evals.suites.behavior._generate_marker", return_value=sentinel_marker):
            run = run_behavior_suite(scenarios=[scenario], transport=EchoTransport(), judge=None)
        for r in _results(run):
            blob = json.dumps(r.details)
            self.assertNotIn(sentinel_marker, blob)
            self.assertNotIn("SENTINEL_REPLY_XYZZY", blob)


# --------------------------------------------------------------------------- #
# Transport factory deferral                                                 #
# --------------------------------------------------------------------------- #
class BuildTransportTest(TestCase):
    @override_settings(DJANGO_BASE_URL="", EVAL_BEHAVIOR_PAT="")
    def test_unwired_transport_raises_named_deferral(self):
        with self.assertRaises(BehaviorConfigError):
            build_behavior_transport(_synthetic_tenant())


# --------------------------------------------------------------------------- #
# Task wrapper                                                               #
# --------------------------------------------------------------------------- #
class BehaviorTaskTest(TestCase):
    def setUp(self):
        self.tenant = _synthetic_tenant()
        self.ctx = override_settings(EVAL_BEHAVIOR_TENANT_ID=str(self.tenant.id), PLATFORM_OWNER_EMAIL="")
        self.ctx.enable()
        self.addCleanup(self.ctx.disable)

    def test_nonpass_run_raises_into_dlq(self):
        # Benign transport creates no cron → the shipped cron_registered fixture FAILS
        # → finalize alerts (owner unset → skipped) then raises into the DLQ.
        with self.assertRaises(RuntimeError):
            eval_behavior_task(transport=BenignTransport(), judge=FakeJudge())

    def test_pass_run_returns_summary(self):
        # Cron-creating transport satisfies cron_registered; benign replies satisfy
        # marker_absent / forbidden_absent / reply_nonempty across all fixtures.
        out = eval_behavior_task(transport=CronCreatingTransport(self.tenant), judge=FakeJudge())
        self.assertEqual(out["suite"], "behavior")
        self.assertEqual(out["status"], EvalRun.Status.PASS)
        self.assertGreater(out["cases"], 0)
