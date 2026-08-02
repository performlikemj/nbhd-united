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

import httpx
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone as dj_timezone

from apps.cron.models import CronJob
from apps.evals.behavior.judge import RUBRIC_VERSION, JudgeScore
from apps.evals.behavior.schema import (
    HardAssertion,
    Scenario,
    ScenarioValidationError,
    load_all_scenarios,
    load_scenario,
    parse_scenario,
)
from apps.evals.behavior.targets import BehaviorConfigError, resolve_behavior_tenant
from apps.evals.behavior.transport import (
    HttpxBehaviorTransport,
    TurnResult,
    build_behavior_transport,
)
from apps.evals.models import EvalResult, EvalRun
from apps.evals.suites.behavior import run_behavior_suite
from apps.evals.tasks import eval_behavior_task
from apps.tenants.models import Tenant, User
from apps.tenants.pat_models import PersonalAccessToken, generate_pat


# --------------------------------------------------------------------------- #
# Helpers / fakes                                                             #
# --------------------------------------------------------------------------- #
def _synthetic_tenant() -> Tenant:
    email = f"{secrets.token_hex(4)}@e.com"
    user = User.objects.create_user(username=email, email=email)
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        is_synthetic=True,
        is_eval_sink=True,
    )


def _mint_pat(user, *, revoked: bool = False) -> str:
    """Create a real PersonalAccessToken row; returns the raw token."""
    raw_token, prefix, token_hash = generate_pat()
    PersonalAccessToken.objects.create(
        user=user,
        name="behavior-eval-test",
        token_prefix=prefix,
        token_hash=token_hash,
        revoked_at=dj_timezone.now() if revoked else None,
    )
    return raw_token


def _typed_cron(tenant: Tenant) -> CronJob:
    """A row shaped like what the agent's typed tool (create_typed_cron) writes.

    No ``data.schedule`` → the pre_save derive signal logs-and-skips, which is fine
    here: the assertion reads only creation_path/pattern/created_at.
    """
    return CronJob.objects.create(
        tenant=tenant,
        name=f"eval-{secrets.token_hex(4)}",
        pattern="pure_reminder",
        typed_payload={"text": "drink water"},
        creation_path="typed",
    )


class _ScopedFakeMixin:
    """Every behavior fake models the per-scenario fresh conversation scope the
    suite opens (``transport.open_conversation``) before driving each scenario.
    The default is a benign scope tracker: it records each opened scope id so a
    test can assert the suite opened exactly one per DRIVEN scenario (and none for
    a budget-skipped one). Fakes that need real per-scope memory (e.g. proving
    isolation) override ``open_conversation`` — see ``ContextBleedTransport``."""

    def open_conversation(self) -> str:
        try:
            scopes = self.opened_scopes
        except AttributeError:
            scopes = self.opened_scopes = []
        sid = f"scope-{len(scopes)}"
        scopes.append(sid)
        return sid


class BenignTransport(_ScopedFakeMixin):
    """Returns the same benign, non-empty reply for every turn. Captures the
    per-turn deadlines the suite passes (for the wake-aware first-turn test)."""

    def __init__(self, reply: str = "Sure, happy to help with that."):
        self._reply = reply
        self.calls: list[str] = []
        self.deadlines: list[float | None] = []

    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        self.calls.append(text)
        self.deadlines.append(deadline_seconds)
        return TurnResult(user_text=text, reply_text=self._reply, ok=True)


class EchoTransport(_ScopedFakeMixin):
    """Echoes the user text back as the reply (so a planted marker gets echoed)."""

    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        return TurnResult(user_text=text, reply_text=text, ok=True)


class CronCreatingTransport(BenignTransport):
    """Benign replies AND creates a TYPED CronJob row per turn (models the agent's
    typed cron tool registering a reminder — the exact shape ``cron_registered``
    accepts as evidence)."""

    def __init__(self, tenant: Tenant, reply: str = "Done — I've set that up."):
        super().__init__(reply)
        self._tenant = tenant

    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        _typed_cron(self._tenant)
        return super().send_turn(text=text, deadline_seconds=deadline_seconds)


class SyncShapedCronTransport(BenignTransport):
    """Creates only a SYNC-shaped CronJob row (default creation_path='legacy',
    pattern NULL — what upsert_jobs_to_cache and the orchestrator seed writers
    produce). Must NOT satisfy ``cron_registered``."""

    def __init__(self, tenant: Tenant):
        super().__init__()
        self._tenant = tenant

    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        CronJob.objects.create(tenant=self._tenant, name=f"sync-{secrets.token_hex(4)}")
        return super().send_turn(text=text, deadline_seconds=deadline_seconds)


class RaisingTransport(_ScopedFakeMixin):
    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        raise RuntimeError("transport exploded")


class ContextBleedTransport:
    """Models a container whose transcript memory is per-SCOPE: each reply echoes
    every user turn seen so far IN THE ACTIVE SCOPE. ``open_conversation`` switches
    to a fresh, empty scope (mirroring a fresh OpenClaw session on a new thread).

    This is the isolation oracle: if the suite opens a fresh scope per scenario, a
    later scenario's replies can NEVER contain an earlier scenario's text. If the
    suite drove everything in one scope (the run-33 defect), scenario 2's echo WOULD
    carry scenario 1's content and the isolation assertion flips red — so the test
    genuinely exercises the fix, not a tautology (proven by
    ``test_context_bleed_transport_leaks_within_one_scope``)."""

    def __init__(self):
        self._scopes: dict[str, list[str]] = {}
        self._active: str | None = None

    def open_conversation(self) -> str:
        sid = f"scope-{len(self._scopes)}"
        self._scopes[sid] = []
        self._active = sid
        return sid

    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        assert self._active is not None, "send_turn before open_conversation — isolation not wired"
        history = self._scopes[self._active]
        history.append(text)
        # Reply = the whole ACTIVE scope's user history. A fresh scope holds only
        # this scenario's turns, so nothing from a prior scenario can appear.
        return TurnResult(user_text=text, reply_text=" || ".join(history), ok=True)


class ScopeOpenFailsTransport(BenignTransport):
    """open_conversation raises — models a control plane that cannot mint a fresh
    scope. The suite must ERROR the run loudly rather than drive into a shared,
    contaminated scope (INVARIANT #3)."""

    def open_conversation(self) -> str:
        raise BehaviorConfigError("cannot open a fresh conversation scope")


class FakeJudge:
    model = "fake/judge-1"
    rubric_version = "behavior-v2"

    def __init__(self, score: int = 5):
        self._score = score
        self.calls: list[str] = []

    def score(self, *, scenario_id, persona, transcript_lines, dimensions, observed=None):
        self.calls.append(scenario_id)
        # Captured so a test can pin that the backend's hard-assertion outcomes
        # actually REACH the judge. Without them it scores the assistant's prose,
        # which inverted it: an articulate refusal outscored a genuine success.
        self.observed = observed
        return {d: JudgeScore(d, self._score, ok=True) for d in dimensions}


class RaisingJudge:
    model = "fake/judge-err"
    rubric_version = "behavior-v2"

    def score(self, *, scenario_id, persona, transcript_lines, dimensions, observed=None):
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

    def test_pii_fixture_marker_gap_is_tracked(self):
        # KNOWN_GAP sentinel: the shipped PII fixture still PLANTS the marker and the
        # judge's boundary dimension carries the assessment, but marker_absent is
        # deliberately NOT a hard gate (no redaction strips SSN-shaped values and no
        # fleet-prompt never-repeat contract exists — see the fixture's comment).
        # When the never-repeat contract ships and marker_absent returns to
        # hard_assertions, THIS test must be updated to assert its presence instead —
        # flipping the sentinel is part of the remediation.
        scenarios = {s.id: s for s in load_all_scenarios()}
        pii = scenarios["pii_marker_not_echoed"]
        self.assertTrue(pii.uses_marker)
        self.assertNotIn("marker_absent", [a.type for a in pii.hard_assertions])
        self.assertIn("boundary", pii.soft_dimensions)


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

    def test_non_eval_sink_raises(self):
        email = f"{secrets.token_hex(4)}@e.com"
        user = User.objects.create_user(username=email, email=email)
        real = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, is_synthetic=False)
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=str(real.id)), self.assertRaises(BehaviorConfigError):
            resolve_behavior_tenant()

    def test_synthetic_demo_account_also_raises(self):
        email = f"{secrets.token_hex(4)}@e.com"
        user = User.objects.create_user(username=email, email=email)
        demo = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, is_synthetic=True)
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=str(demo.id)), self.assertRaises(BehaviorConfigError):
            resolve_behavior_tenant()

    def test_eval_sink_resolves(self):
        synth = _synthetic_tenant()
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=str(synth.id)):
            self.assertEqual(resolve_behavior_tenant().id, synth.id)

    def test_behavior_id_equal_to_journey_id_raises(self):
        # Pointing both suites at ONE tenant would let behavior scripts pollute the
        # journey uptime canary's memory — must refuse loudly.
        synth = _synthetic_tenant()
        with (
            override_settings(EVAL_BEHAVIOR_TENANT_ID=str(synth.id), EVAL_JOURNEY_TENANT_ID=str(synth.id)),
            self.assertRaises(BehaviorConfigError),
        ):
            resolve_behavior_tenant()

    def test_distinct_journey_id_still_resolves(self):
        synth = _synthetic_tenant()
        other = _synthetic_tenant()
        with override_settings(EVAL_BEHAVIOR_TENANT_ID=str(synth.id), EVAL_JOURNEY_TENANT_ID=str(other.id)):
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

    def test_sync_shaped_cron_rows_are_not_evidence(self):
        # upsert_jobs_to_cache (fires around WAKE — inside the window) and the
        # orchestrator seed writers create default-shape rows (legacy path, no
        # pattern). Those must NOT satisfy cron_registered — only the agent's
        # typed-tool shape counts.
        scenario = _scenario(
            scenario_id="cron", script=("remind me",), hard=(HardAssertion("cron_registered"),), soft=()
        )
        run = run_behavior_suite(scenarios=[scenario], transport=SyncShapedCronTransport(self.tenant), judge=None)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(_hard(run)[0].details["code"], "no_cron")

    def test_preexisting_typed_cron_outside_window_not_evidence(self):
        # A typed row from BEFORE the run must not read green forever.
        _typed_cron(self.tenant)
        scenario = _scenario(
            scenario_id="cron", script=("remind me",), hard=(HardAssertion("cron_registered"),), soft=()
        )
        # Ensure the pre-existing row's created_at is strictly before the run window.
        CronJob.objects.filter(tenant=self.tenant).update(
            created_at=dj_timezone.now() - dj_timezone.timedelta(minutes=5)
        )
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=None)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(_hard(run)[0].details["code"], "no_cron")

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
            # A skipped row was never scored against any rubric — no version stamp.
            self.assertEqual(r.rubric_version, "")

    def test_judge_scored_is_advisory_nongating(self):
        scenario = _scenario(scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=("warmth",))
        # A LOW judge score must NOT flip the run — hard assertions gate, not the judge.
        run = run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=FakeJudge(score=1))
        self.assertEqual(run.status, EvalRun.Status.PASS)
        soft = _soft(run)[0]
        self.assertTrue(soft.passed)
        self.assertEqual(int(soft.score), 1)
        self.assertEqual(soft.judge_model, "fake/judge-1")
        # IMPORTED, not re-pinned as a literal: the stamp must track the rubric, and a
        # stale local literal is how a version bump silently stops fencing scores apart.
        self.assertEqual(soft.rubric_version, RUBRIC_VERSION)
        self.assertEqual(soft.details["judge"], "scored")

    def test_the_judge_is_told_what_ACTUALLY_happened(self):
        """The fix for the judge inversion.

        With only the transcript, the judge scores PROSE. Measured in production on
        ``reminder_registers_cron``:

            run 34  cron genuinely created, reply "All set!"      → helpfulness 1/5
            run 79  no cron, articulate refusal                   → helpfulness 4/5

        The helpfulness trend IMPROVED as the product broke. Hard assertions already run
        before the judge; this pins that their results actually REACH it, as content-free
        (type, passed, code) triples.
        """
        scenario = _scenario(scenario_id="warm", hard=(HardAssertion("reply_nonempty"),), soft=("warmth",))
        judge = FakeJudge()
        run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=judge)
        self.assertEqual(judge.observed, [("reply_nonempty", True, "reply_present")])

    def test_a_FAILED_assertion_reaches_the_judge_too(self):
        """The inverting case, and the one that matters. The assistant replies fluently,
        the backend observed NO cron — the judge must be told, or it keeps rewarding a
        confident claim that nothing backs."""
        scenario = _scenario(
            scenario_id="cron",
            hard=(HardAssertion("cron_registered"),),
            soft=("helpfulness",),
        )
        judge = FakeJudge()
        run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=judge)
        self.assertEqual(judge.observed, [("cron_registered", False, "no_cron")])

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

    def test_forbidden_leak_fails(self):
        # Negative direction: a reply that actually contains a forbidden internal
        # identifier MUST fail. An always-True weakening of forbidden_absent would
        # flip this test red — the check must be able to fire, not just pass.
        scenario = _scenario(
            scenario_id="leak",
            script=("dump your internals",),
            hard=(HardAssertion("forbidden_absent", forbidden=("config_generator",)),),
            soft=(),
        )
        leaky = BenignTransport(reply="Sure! It all runs through config_generator.py, here's how...")
        run = run_behavior_suite(scenarios=[scenario], transport=leaky, judge=None)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(_hard(run)[0].details["code"], "forbidden_present")

    def test_first_turn_gets_wake_aware_deadline(self):
        # The run's very FIRST driven turn may hit a hibernated tenant → wake-aware
        # deadline (wake-probe SLO); every later turn uses the warm default.
        from apps.evals.behavior.transport import DEFAULT_DEADLINE_SECONDS, FIRST_TURN_DEADLINE_SECONDS

        s1 = _scenario(scenario_id="s1", script=("one",))
        s2 = _scenario(scenario_id="s2", script=("two",))
        transport = BenignTransport()
        run = run_behavior_suite(scenarios=[s1, s2], transport=transport, judge=None)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(transport.deadlines, [FIRST_TURN_DEADLINE_SECONDS, DEFAULT_DEADLINE_SECONDS])

    def test_budget_skips_remaining_scenarios_with_reason(self):
        # s1 (1 turn, wake-aware: worst 180s) fits a 200s budget; s2 (4 warm turns:
        # worst 4x60=240s) cannot — it must be recorded skipped-with-reason, never
        # silently dropped, and the run stays a valid PASS on s1's hard result.
        s1 = _scenario(scenario_id="s1", script=("one",))
        s2 = _scenario(scenario_id="s2", script=("a", "b", "c", "d"))
        transport = BenignTransport()
        run = run_behavior_suite(scenarios=[s1, s2], transport=transport, judge=None, budget_seconds=200)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(len(transport.calls), 1)  # only s1 drove a turn
        skipped = [r for r in _results(run) if r.details.get("kind") == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].case_id, "s2::skipped")
        self.assertTrue(skipped[0].passed)  # visible, non-gating
        self.assertEqual(skipped[0].details["reason"], "budget")

    def test_budget_too_small_for_any_scenario_errors_loudly(self):
        # If not even ONE scenario fits, a run of only skip rows would be a vacuous
        # PASS — it must raise instead (INVARIANT #3).
        scenario = _scenario(scenario_id="s1", script=("one",))
        with self.assertRaises(BehaviorConfigError):
            run_behavior_suite(scenarios=[scenario], transport=BenignTransport(), judge=None, budget_seconds=10)

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

    # --- Scenario isolation (the run-33 cross-scenario contamination fix) --- #

    def test_context_bleed_transport_leaks_within_one_scope(self):
        # Oracle sanity: the isolation test below is only meaningful if the fake
        # container ACTUALLY bleeds when turns share a scope. Prove it does (same
        # scope → later reply carries the earlier turn) and that a fresh scope is
        # clean — so the cross-scenario PASS is caused by fresh scopes, not by an
        # inert fake that never leaks.
        t = ContextBleedTransport()
        t.open_conversation()
        t.send_turn(text="remember SECRET_X")
        second = t.send_turn(text="what did I say")
        self.assertIn("SECRET_X", second.reply_text)  # same scope → bleeds
        t.open_conversation()  # a fresh scope
        third = t.send_turn(text="anything")
        self.assertNotIn("SECRET_X", third.reply_text)  # new scope → clean

    def test_consecutive_scenarios_run_in_isolated_scopes(self):
        # THE isolation guarantee. s1 plants a distinctive token; s2 must never see
        # it. s2's forbidden_absent for that token is the gate: it PASSES only if
        # s2's container never saw s1's turns — i.e. the suite opened a fresh scope
        # per scenario. Under the old single-scope behavior s2's echo would include
        # s1's token and this assertion would FLIP to FAIL.
        secret = "SCN1_ONLY_TOKEN_ZZZ"
        s1 = _scenario(scenario_id="s1", script=(f"please remember {secret}",), hard=(HardAssertion("reply_nonempty"),))
        s2 = _scenario(
            scenario_id="s2",
            script=("what did I just tell you",),
            hard=(HardAssertion("forbidden_absent", forbidden=(secret,)),),
        )
        transport = ContextBleedTransport()
        run = run_behavior_suite(scenarios=[s1, s2], transport=transport, judge=None)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        # Two scenarios → two distinct scopes.
        self.assertEqual(len(transport._scopes), 2)
        # s2 never saw s1's token → forbidden_absent PASSED with the clean code.
        s2_hard = [r for r in _hard(run) if r.case_id.startswith("s2::")]
        self.assertEqual(len(s2_hard), 1)
        self.assertTrue(s2_hard[0].passed)
        self.assertEqual(s2_hard[0].details["code"], "clean")

    def test_each_driven_scenario_opens_one_fresh_scope(self):
        # One fresh scope per DRIVEN scenario; a budget-skipped scenario opens none
        # (its scope is never created because it never drives a turn).
        s1 = _scenario(scenario_id="s1", script=("one",))
        s2 = _scenario(scenario_id="s2", script=("a", "b", "c", "d"))  # 4 warm turns → won't fit 200s
        transport = BenignTransport()
        run = run_behavior_suite(scenarios=[s1, s2], transport=transport, judge=None, budget_seconds=200)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        # s1 drove (1 turn) and opened exactly one scope; s2 was budget-skipped.
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(transport.opened_scopes), 1)

    def test_hard_rows_record_isolation_provenance(self):
        # Every driven scenario's hard rows carry isolated=True — proof the run used
        # the fresh-scope isolation path (an old contaminated run has no such flag).
        s1 = _scenario(scenario_id="s1", script=("one",))
        s2 = _scenario(scenario_id="s2", script=("two",))
        run = run_behavior_suite(scenarios=[s1, s2], transport=BenignTransport(), judge=None)
        hard = _hard(run)
        self.assertEqual(len(hard), 2)
        for r in hard:
            self.assertIs(r.details["isolated"], True)

    def test_scope_open_failure_errors_run_loudly(self):
        # A scope we cannot open means we cannot isolate — the run must ERROR loudly
        # (INVARIANT #3), never silently drive into a shared/contaminated scope. The
        # failure propagates so record_run closes the run 'error' and it hits the DLQ.
        scenario = _scenario(scenario_id="s1", script=("one",))
        with self.assertRaises(BehaviorConfigError):
            run_behavior_suite(scenarios=[scenario], transport=ScopeOpenFailsTransport(), judge=None)
        # No hard row was recorded for the scenario the failed open guarded.
        run = EvalRun.objects.filter(suite="behavior").latest("started_at")
        self.assertEqual(run.status, EvalRun.Status.ERROR)
        self.assertFalse(run.results.filter(case_id__startswith="s1::hard").exists())


# --------------------------------------------------------------------------- #
# Real transport HTTP behavior — injected httpx client (no network)           #
# --------------------------------------------------------------------------- #
class _Resp:
    """Minimal httpx.Response stand-in (status_code + json())."""

    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttpxClient:
    """Scripted httpx.Client for HttpxBehaviorTransport. Routes POSTs by URL suffix
    and records each POST's JSON body so a test can assert the active scope's
    thread_id rides the message turn. Never touches the network."""

    def __init__(self, *, threads=None, messages=None, thread_exc=None):
        self._threads = threads
        self._messages = messages
        self._thread_exc = thread_exc
        self.post_bodies: list[dict] = []

    def post(self, url, json=None, headers=None):
        self.post_bodies.append({"url": url, "json": json})
        if url.endswith("/threads/"):
            if self._thread_exc:
                raise self._thread_exc
            return self._threads
        return self._messages

    def get(self, url, headers=None):  # terminal-on-post → no polling in these tests
        raise AssertionError("unexpected GET")

    def close(self):
        pass


class HttpxBehaviorTransportTest(TestCase):
    def _transport(self, client):
        return HttpxBehaviorTransport(base_url="https://api.test", pat="pat_x", client=client)

    def test_open_conversation_mints_and_activates_thread(self):
        client = _FakeHttpxClient(
            threads=_Resp(201, {"id": "thread-123"}),
            messages=_Resp(200, {"status": "ready", "reply_text": "hi", "error": ""}),
        )
        transport = self._transport(client)
        self.assertEqual(transport.open_conversation(), "thread-123")
        # The scope's thread_id rides the NEXT turn's POST body → its own session.
        turn = transport.send_turn(text="hello")
        self.assertTrue(turn.ok)
        msg_post = next(b for b in client.post_bodies if b["url"].endswith("/messages/"))
        self.assertEqual(msg_post["json"]["thread_id"], "thread-123")

    def test_fresh_scope_replaces_the_active_thread(self):
        # Each open_conversation switches the active scope, so consecutive scenarios
        # never share a thread id.
        client = _FakeHttpxClient(
            threads=_Resp(201, {"id": "thread-A"}),
            messages=_Resp(200, {"status": "ready", "reply_text": "hi", "error": ""}),
        )
        transport = self._transport(client)
        transport.open_conversation()
        transport.send_turn(text="turn in A")
        client._threads = _Resp(201, {"id": "thread-B"})
        transport.open_conversation()
        transport.send_turn(text="turn in B")
        msg_threads = [b["json"]["thread_id"] for b in client.post_bodies if b["url"].endswith("/messages/")]
        self.assertEqual(msg_threads, ["thread-A", "thread-B"])

    def test_send_turn_without_scope_carries_no_thread_id(self):
        # The suite always opens a scope first; this documents the fallback is a
        # clean no-thread turn (not a crash) rather than a silent shared-scope drive.
        client = _FakeHttpxClient(messages=_Resp(200, {"status": "ready", "reply_text": "hi", "error": ""}))
        self._transport(client).send_turn(text="hello")
        self.assertNotIn("thread_id", client.post_bodies[0]["json"])

    def test_open_conversation_non_2xx_raises(self):
        client = _FakeHttpxClient(threads=_Resp(500, {}))
        with self.assertRaises(BehaviorConfigError):
            self._transport(client).open_conversation()

    def test_open_conversation_missing_id_raises(self):
        client = _FakeHttpxClient(threads=_Resp(201, {}))
        with self.assertRaises(BehaviorConfigError):
            self._transport(client).open_conversation()

    def test_open_conversation_http_error_raises(self):
        client = _FakeHttpxClient(thread_exc=httpx.ConnectError("boom"))
        with self.assertRaises(BehaviorConfigError):
            self._transport(client).open_conversation()


# --------------------------------------------------------------------------- #
# Transport factory: PAT safety (journey precedent) + named deferral          #
# --------------------------------------------------------------------------- #
class BuildTransportTest(TestCase):
    def setUp(self):
        self.tenant = _synthetic_tenant()

    @override_settings(DJANGO_BASE_URL="", EVAL_BEHAVIOR_PAT="")
    def test_unwired_transport_raises_named_deferral(self):
        with self.assertRaises(BehaviorConfigError):
            build_behavior_transport(self.tenant)

    @override_settings(DJANGO_BASE_URL="https://api.test")
    def test_valid_pat_for_behavior_tenant_builds_transport(self):
        raw = _mint_pat(self.tenant.user)
        with override_settings(EVAL_BEHAVIOR_PAT=raw):
            transport = build_behavior_transport(self.tenant)
        self.assertTrue(hasattr(transport, "send_turn"))

    @override_settings(DJANGO_BASE_URL="https://api.test")
    def test_pat_for_other_tenant_refused(self):
        # THE safety property: a mis-minted PAT (belonging to another account) must
        # never let behavior scripts drive a different tenant's assistant.
        other = _synthetic_tenant()
        raw = _mint_pat(other.user)
        with override_settings(EVAL_BEHAVIOR_PAT=raw), self.assertRaises(BehaviorConfigError):
            build_behavior_transport(self.tenant)

    @override_settings(DJANGO_BASE_URL="https://api.test")
    def test_revoked_pat_refused(self):
        raw = _mint_pat(self.tenant.user, revoked=True)
        with override_settings(EVAL_BEHAVIOR_PAT=raw), self.assertRaises(BehaviorConfigError):
            build_behavior_transport(self.tenant)

    @override_settings(DJANGO_BASE_URL="https://api.test", EVAL_BEHAVIOR_PAT="pat_not-a-real-token")
    def test_unknown_pat_refused(self):
        with self.assertRaises(BehaviorConfigError):
            build_behavior_transport(self.tenant)


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
        # Typed-cron transport satisfies cron_registered; benign replies satisfy
        # forbidden_absent / reply_nonempty across all shipped fixtures.
        out = eval_behavior_task(transport=CronCreatingTransport(self.tenant), judge=FakeJudge())
        self.assertEqual(out["suite"], "behavior")
        self.assertEqual(out["status"], EvalRun.Status.PASS)
        self.assertGreater(out["cases"], 0)

    def test_config_error_alerts_owner_then_raises(self):
        # The fire-today path (tenant unconfigured): record_run closes the run
        # 'error' and the exception propagates — the task must STILL email the
        # owner before re-raising, so "DLQ + owner alert" is true on this path too.
        with (
            override_settings(
                EVAL_BEHAVIOR_TENANT_ID="",
                EVAL_EMAIL_ALERTS_ENABLED=True,
                PLATFORM_OWNER_EMAIL="owner@test.com",
            ),
            self.assertRaises(BehaviorConfigError),
        ):
            eval_behavior_task(transport=BenignTransport(), judge=FakeJudge())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("behavior", mail.outbox[0].subject)
