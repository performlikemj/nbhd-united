"""Suite 2 — model-behavior evals (docs/evals-directive.md §Suite 2).

Drives each YAML scenario against the synthetic behavior tenant's container, then:
  * runs the scenario's DETERMINISTIC hard assertions against OBSERVABLE evidence
    (DB side-effect rows + reply-text patterns) — these GATE the run pass/fail;
  * scores the subjective soft dimensions with the pinned, spend-capped judge —
    these are ADVISORY (non-gating), the "advisory gate first" NAMED decision.

Advisory soft dimensions are recorded ``passed=True`` ALWAYS so they never flip the
run — the real signal is the 1-5 ``score`` (stamped with ``judge_model`` +
``rubric_version``), which trend queries read. This is NOT fake-green: a low judge
score is recorded HONESTLY in the score column; the run's pass/fail is gated only by
the deterministic hard assertions. When the judge is unavailable (unconfigured) or a
scenario is past the per-run judge cap, the soft dimension is recorded SKIPPED WITH A
REASON (``score=None``, a reason code in details) — never silently, never as green.

INVARIANT #1: the reply text drives assertions + judge but is NEVER written to
details or logged. ``details`` here carries only counts / codes / labels.
INVARIANT #8: ``record_run`` opens no transaction; every httpx call runs outside any
``atomic()`` (drive the turn, THEN record).
"""

from __future__ import annotations

import logging
import secrets

from apps.evals.behavior.assertions import run_hard_assertion
from apps.evals.behavior.judge import RUBRIC_VERSION, Judge, JudgeScore, build_default_judge
from apps.evals.behavior.schema import MARKER_TOKEN, Scenario, load_all_scenarios
from apps.evals.behavior.targets import BehaviorConfigError, resolve_behavior_tenant
from apps.evals.behavior.transport import (
    BehaviorTransport,
    ScenarioRun,
    TurnResult,
    build_behavior_transport,
    reset_behavior_workspace,
)
from apps.evals.models import EvalResult, EvalRun
from apps.evals.runner import record, record_run

logger = logging.getLogger(__name__)

SUITE = "behavior"

# Per-run judge cap (spend guard): after this many scenarios have been judged in a
# single run, remaining soft dimensions are recorded SKIPPED with reason
# ``scenario_cap`` — bounding OpenRouter spend deterministically.
MAX_JUDGED_SCENARIOS_PER_RUN = 12

# Sentinel distinguishing "caller did not pass a judge → build the default" from
# "caller explicitly passed ``judge=None`` → force judge-off (skip soft dims)".
_JUDGE_UNSET = object()


def _generate_marker() -> str:
    """A fresh synthetic fake-PII marker, shaped like a US SSN so the tenant's
    redaction pipeline is expected to strip it before it can be echoed. Random per
    run, so a prior run's residue can never make ``marker_absent`` read green (or
    red) — this is why per-scenario workspace reset is not required for correctness."""
    n1 = 100 + secrets.randbelow(800)  # 100-899
    n2 = 10 + secrets.randbelow(90)  # 10-99
    n3 = 1000 + secrets.randbelow(9000)  # 1000-9999
    return f"{n1}-{n2}-{n3}"


def _drive_scenario(scenario: Scenario, transport: BehaviorTransport) -> ScenarioRun:
    """Drive one scenario's multi-turn script; collect the (synthetic) replies.

    A transport error on a turn is captured as a failed ``TurnResult`` (not raised),
    so it surfaces as a failed hard assertion — a clean run FAIL with a code, not a
    run ERROR. ``started_at`` is stamped BEFORE the first turn so ``cron_registered``
    windows only side effects this run caused.
    """
    from apps.evals.behavior.transport import now

    marker = _generate_marker() if scenario.uses_marker else ""
    run = ScenarioRun(scenario_id=scenario.id, marker=marker, started_at=now())
    for line in scenario.script:
        text = line.replace(MARKER_TOKEN, marker) if marker else line
        try:
            turn = transport.send_turn(text=text)
        except Exception:  # noqa: BLE001 — a transport blip is a failed turn, not a run ERROR
            logger.warning("behavior: transport raised on scenario %s (recorded as failed turn)", scenario.id)
            turn = TurnResult(user_text=text, ok=False, error="transport_exception")
        run.turns.append(turn)
    return run


def _transcript_lines(scenario: Scenario, run: ScenarioRun) -> list[tuple[str, str]]:
    """Interleave the synthetic user turns and assistant replies for the judge."""
    lines: list[tuple[str, str]] = []
    for turn in run.turns:
        lines.append(("user", turn.user_text))
        lines.append(("assistant", turn.reply_text))
    return lines


def _safe_judge(judge: Judge, scenario: Scenario, run: ScenarioRun) -> dict[str, JudgeScore]:
    """Call the judge, converting ANY failure into skipped-with-reason scores — a
    judge crash must never fail the (advisory) run."""
    dims = list(scenario.soft_dimensions)
    try:
        return judge.score(
            scenario_id=scenario.id,
            persona=scenario.persona,
            transcript_lines=_transcript_lines(scenario, run),
            dimensions=dims,
        )
    except Exception:  # noqa: BLE001 — advisory: never propagate a judge failure
        logger.warning("behavior: judge raised on scenario %s (soft dims skipped)", scenario.id)
        return {d: JudgeScore(d, None, ok=False, reason="judge_error") for d in dims}


def _record_hard(run: EvalRun, scenario: Scenario, assertion_type: str, passed: bool, code: str, turns: int) -> None:
    record(
        run,
        f"{scenario.id}::hard:{assertion_type}",
        EvalResult.Kind.BEHAVIOR,
        passed=passed,
        details={"kind": "hard", "assertion": assertion_type, "code": code, "turns": turns},
    )


def _record_soft(
    run: EvalRun,
    scenario: Scenario,
    dim: str,
    *,
    score: int | None,
    status: str,
    reason: str,
    judge_model: str,
) -> None:
    """Record ONE advisory soft-dimension row. ``passed=True`` ALWAYS (non-gating);
    the honest signal is ``score`` + the ``status``/``reason`` codes. Never content."""
    record(
        run,
        f"{scenario.id}::soft:{dim}",
        EvalResult.Kind.BEHAVIOR,
        passed=True,  # ADVISORY — soft dims never gate the run (directive §Suite 2)
        score=score,
        details={"kind": "soft", "dim": dim, "judge": status, "reason": reason},
        judge_model=judge_model,
        rubric_version=RUBRIC_VERSION,
    )


def run_behavior_suite(
    *,
    scenarios: list[Scenario] | None = None,
    transport: BehaviorTransport | None = None,
    judge=_JUDGE_UNSET,
    max_judged_scenarios: int = MAX_JUDGED_SCENARIOS_PER_RUN,
    trigger: str = EvalRun.Trigger.MANUAL,
) -> EvalRun:
    """Run the behavior suite and return the CLOSED run.

    Resolution (tenant / transport / judge / scenarios) happens INSIDE
    ``record_run`` so a misconfiguration closes the run ``error`` and re-raises into
    the DLQ (INVARIANT #3 — a suite that cannot run FAILS loudly, never silently
    passes). ``transport``/``judge``/``scenarios`` are injectable for tests; in
    production they default to the real container transport, the pinned judge, and
    the YAML fixtures.

    ``judge`` is a tri-state: unset → build the default judge; ``None`` → force
    judge-off (soft dims skipped-with-reason); a Judge → use it.
    """
    with record_run(SUITE, trigger) as run:  # runtime suite → image_tag auto-infers the fleet tag
        tenant = resolve_behavior_tenant()
        scenario_list = list(scenarios) if scenarios is not None else load_all_scenarios()
        if not scenario_list:
            # Zero scenarios would record zero cases → a vacuous 'error' close. Fail
            # loudly with a clear cause instead (INVARIANT #3).
            raise BehaviorConfigError("no behavior scenarios found — the suite would assert nothing")

        active_transport = transport if transport is not None else build_behavior_transport(tenant)
        active_judge: Judge | None = build_default_judge() if judge is _JUDGE_UNSET else judge

        judged_count = 0
        for scenario in scenario_list:
            scenario_run = _drive_scenario(scenario, active_transport)
            n_turns = len(scenario_run.turns)

            # Hard assertions — GATING.
            for assertion in scenario.hard_assertions:
                passed, code = run_hard_assertion(scenario_run, tenant, assertion)
                _record_hard(run, scenario, assertion.type, passed, code, n_turns)

            # Soft dimensions — ADVISORY (non-gating).
            if scenario.soft_dimensions:
                if active_judge is None:
                    for dim in scenario.soft_dimensions:
                        _record_soft(
                            run,
                            scenario,
                            dim,
                            score=None,
                            status="skipped",
                            reason="judge_unconfigured",
                            judge_model="",
                        )
                elif judged_count >= max_judged_scenarios:
                    for dim in scenario.soft_dimensions:
                        _record_soft(
                            run, scenario, dim, score=None, status="skipped", reason="scenario_cap", judge_model=""
                        )
                else:
                    scores = _safe_judge(active_judge, scenario, scenario_run)
                    judged_count += 1
                    jm = getattr(active_judge, "model", "")
                    for dim in scenario.soft_dimensions:
                        js = scores.get(dim) or JudgeScore(dim, None, ok=False, reason="missing_dimension")
                        if js.ok:
                            _record_soft(run, scenario, dim, score=js.score, status="scored", reason="", judge_model=jm)
                        else:
                            _record_soft(
                                run, scenario, dim, score=None, status="skipped", reason=js.reason, judge_model=jm
                            )

            # Between scenarios: NAMED DEFERRAL (container-side reset). See docstring.
            reset_behavior_workspace(tenant, scenario_run)

    return run
