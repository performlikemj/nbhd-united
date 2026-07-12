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

READ-PATH NOTE for Wave E and any pass-rate query: behavior rows MUST be sliced on
``details->kind`` (``hard`` / ``soft`` / ``skipped``). Soft and skipped rows are
``passed=True`` BY DESIGN (advisory), so an unsliced pass-rate over behavior rows
reads greener than reality — only ``kind='hard'`` rows carry the gating signal.

EXECUTION BUDGET (fact #2 of docs/evals-wave-b-plan.md — the ~300s gunicorn worker
ceiling): the whole run executes inline in one QStash-triggered request. A full pack
worst case (many 60s turn deadlines + judge calls) would blow far past 300s → the
worker is SIGKILL'd mid-run, the EvalRun row strands at 'running' (reaper fodder),
and QStash re-fires the WHOLE suite. So the run anchors a total wall-clock budget at
t0 (``SUITE_BUDGET_SECONDS``, comfortably under 300s) and stops STARTING new
scenarios once the remaining budget cannot fit the next scenario's worst case
(per-turn deadlines + judge timeout). Unrun scenarios are recorded skipped-with-
reason ``budget`` — never silently. In practice warm turns finish in seconds so the
whole pack fits; the budget only bites when things are genuinely slow. NAMED
FOLLOW-UP when the pack grows: per-scenario fan-out (one QStash task per scenario),
which removes the shared ceiling entirely.

The run's FIRST turn may find the behavior tenant hibernated (cold start regularly
past 2 min), so it gets a wake-aware deadline (``FIRST_TURN_DEADLINE_SECONDS``,
aligned with the wake probe's SLO); every later turn is warm and uses the default.

INVARIANT #1: the reply text drives assertions + judge but is NEVER written to
details or logged. ``details`` here carries only counts / codes / labels.
INVARIANT #8: ``record_run`` opens no transaction; every httpx call runs outside any
``atomic()`` (drive the turn, THEN record).
"""

from __future__ import annotations

import logging
import secrets
import time

from apps.evals.behavior.assertions import run_hard_assertion
from apps.evals.behavior.judge import (
    JUDGE_TIMEOUT_SECONDS,
    RUBRIC_VERSION,
    Judge,
    JudgeScore,
    build_default_judge,
)
from apps.evals.behavior.schema import MARKER_TOKEN, Scenario, load_all_scenarios
from apps.evals.behavior.targets import BehaviorConfigError, resolve_behavior_tenant
from apps.evals.behavior.transport import (
    DEFAULT_DEADLINE_SECONDS,
    FIRST_TURN_DEADLINE_SECONDS,
    BehaviorTransport,
    ScenarioRun,
    TurnResult,
    build_behavior_transport,
)
from apps.evals.models import EvalResult, EvalRun
from apps.evals.runner import record, record_run

logger = logging.getLogger(__name__)

SUITE = "behavior"

# Total wall-clock budget for one suite fire, 15s under the 300s gunicorn worker
# ceiling (config/settings/base.py) so the run always closes cleanly instead of
# being SIGKILL'd mid-scenario. The budget gates when scenarios START; per-turn
# HTTP deadlines + the judge timeout bound everything inside a scenario, so once
# the last gated scenario finishes (≤ budget by construction) only millisecond
# bookkeeping (record/close/alert DB writes) remains — 15s of headroom is ample.
# The arithmetic must admit the shipped pack's LARGEST first scenario: a 2-turn
# scenario driven first is 5 (fresh-scope open) + 180 (wake-aware turn) + 60 (warm
# turn) + 30 (judge) = 275 ≤ 285. Worst-case gating means a slow run drives fewer
# scenarios and budget-skips the rest — honest and visible, never a stranded
# 'running' row.
SUITE_BUDGET_SECONDS = 285.0

# Per-run judge cap (spend guard): after this many scenarios have been judged in a
# single run, remaining soft dimensions are recorded SKIPPED with reason
# ``scenario_cap`` — bounding OpenRouter spend deterministically.
MAX_JUDGED_SCENARIOS_PER_RUN = 12

# Opening a fresh conversation scope (POST /chat/threads/) before each scenario is
# a control-plane DB insert reached over a loopback round trip — tens of ms in
# practice. We reserve a small worst-case allowance in the budget gate so the
# arithmetic stays honest, without reserving the full HTTP timeout per scenario
# (that would pathologically over-skip). A scope open slower than this on a
# still-alive control plane is absorbed by the suite's 15s budget headroom, and a
# control plane sicker than that is already failing the turns themselves.
SCOPE_OPEN_BUDGET_SECONDS = 5.0

# Sentinel distinguishing "caller did not pass a judge → build the default" from
# "caller explicitly passed ``judge=None`` → force judge-off (skip soft dims)".
_JUDGE_UNSET = object()


def _generate_marker() -> str:
    """A fresh synthetic fake-PII marker, shaped like a US SSN.

    Reality (KNOWN_GAP, see the pii_marker_not_echoed fixture): the PII engine
    deliberately does NOT redact bare SSN-shaped values (apps/pii/config.py), so
    this marker reaches the model RAW — what the marker probes is whether the
    ASSISTANT repeats it back, currently assessed by the judge's boundary dimension
    (hard gate returns when the fleet-prompt never-repeat contract ships). Random
    per run, so a prior run's residue can never make a marker check read green (or
    red) — this keeps the HARD checks residue-immune without a container-side memory
    reset. (Conversation-TRANSCRIPT independence between scenarios is a separate
    concern, and IS enforced: the suite opens a fresh conversation scope per
    scenario — see run_behavior_suite / transport.open_conversation.)
    """
    n1 = 100 + secrets.randbelow(800)  # 100-899
    n2 = 10 + secrets.randbelow(90)  # 10-99
    n3 = 1000 + secrets.randbelow(9000)  # 1000-9999
    return f"{n1}-{n2}-{n3}"


def _drive_scenario(scenario: Scenario, transport: BehaviorTransport, *, wake_aware_first_turn: bool) -> ScenarioRun:
    """Drive one scenario's multi-turn script; collect the (synthetic) replies.

    A transport error on a turn is captured as a failed ``TurnResult`` (not raised),
    so it surfaces as a failed hard assertion — a clean run FAIL with a code, not a
    run ERROR. ``started_at`` is stamped BEFORE the first turn so ``cron_registered``
    windows only side effects this run caused. When ``wake_aware_first_turn`` is set
    (the run's very first driven turn), that turn gets the wake-aware deadline.
    """
    from apps.evals.behavior.transport import now

    marker = _generate_marker() if scenario.uses_marker else ""
    run = ScenarioRun(scenario_id=scenario.id, marker=marker, started_at=now())
    for i, line in enumerate(scenario.script):
        text = line.replace(MARKER_TOKEN, marker) if marker else line
        deadline = FIRST_TURN_DEADLINE_SECONDS if (wake_aware_first_turn and i == 0) else DEFAULT_DEADLINE_SECONDS
        try:
            turn = transport.send_turn(text=text, deadline_seconds=deadline)
        except Exception:  # noqa: BLE001 — a transport blip is a failed turn, not a run ERROR
            logger.warning("behavior: transport raised on scenario %s (recorded as failed turn)", scenario.id)
            turn = TurnResult(user_text=text, ok=False, error="transport_exception")
        run.turns.append(turn)
    return run


def _scenario_worst_case_seconds(scenario: Scenario, *, wake_aware_first_turn: bool, will_judge: bool) -> float:
    """Worst-case wall clock for one scenario: the fresh-scope open, the sum of its
    per-turn poll deadlines, plus (when it would be judged) the judge call's
    timeout. Used to gate STARTING a scenario against the remaining run budget."""
    total = SCOPE_OPEN_BUDGET_SECONDS  # a fresh conversation scope is opened first
    for i in range(len(scenario.script)):
        total += FIRST_TURN_DEADLINE_SECONDS if (wake_aware_first_turn and i == 0) else DEFAULT_DEADLINE_SECONDS
    if will_judge:
        total += float(JUDGE_TIMEOUT_SECONDS)
    return total


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
    # ``isolated=True`` is per-scenario isolation provenance: this scenario ran in
    # its OWN freshly-opened conversation scope (see run_behavior_suite). A driven
    # scenario always reaches here after a successful open_conversation (a failed
    # open ERRORs the run before any hard row), so the flag records that isolation
    # HELD — a scalar bool, content-free, safe past the record() details cap. A
    # future reader can slice on it to confirm a run used the isolation path (an
    # old contaminated run's rows carry no such flag).
    record(
        run,
        f"{scenario.id}::hard:{assertion_type}",
        EvalResult.Kind.BEHAVIOR,
        passed=passed,
        details={"kind": "hard", "assertion": assertion_type, "code": code, "turns": turns, "isolated": True},
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
    rubric_version: str,
) -> None:
    """Record ONE advisory soft-dimension row. ``passed=True`` ALWAYS (non-gating);
    the honest signal is ``score`` + the ``status``/``reason`` codes. Never content.
    ``rubric_version`` is stamped ONLY on scored rows — a skipped row was never
    scored against any rubric, so stamping one would be a false provenance claim."""
    record(
        run,
        f"{scenario.id}::soft:{dim}",
        EvalResult.Kind.BEHAVIOR,
        passed=True,  # ADVISORY — soft dims never gate the run (directive §Suite 2)
        score=score,
        details={"kind": "soft", "dim": dim, "judge": status, "reason": reason},
        judge_model=judge_model,
        rubric_version=rubric_version,
    )


def _record_scenario_skipped(run: EvalRun, scenario: Scenario, reason: str) -> None:
    """Record one visible row for a scenario the run did NOT drive (e.g. out of
    budget). ``passed=True`` so it never gates, but ``details.kind='skipped'`` +
    the reason code make it unmistakable in any sliced query — never a silent drop,
    and never counted as a proven hard assertion (distinct case id + kind)."""
    record(
        run,
        f"{scenario.id}::skipped",
        EvalResult.Kind.BEHAVIOR,
        passed=True,
        details={"kind": "skipped", "reason": reason, "turns": 0},
    )


def run_behavior_suite(
    *,
    scenarios: list[Scenario] | None = None,
    transport: BehaviorTransport | None = None,
    judge=_JUDGE_UNSET,
    max_judged_scenarios: int = MAX_JUDGED_SCENARIOS_PER_RUN,
    budget_seconds: float = SUITE_BUDGET_SECONDS,
    trigger: str = EvalRun.Trigger.MANUAL,
) -> EvalRun:
    """Run the behavior suite and return the CLOSED run.

    Resolution (tenant / transport / judge / scenarios) happens INSIDE
    ``record_run`` so a misconfiguration closes the run ``error`` and re-raises into
    the DLQ (INVARIANT #3 — a suite that cannot run FAILS loudly, never silently
    passes). ``transport``/``judge``/``scenarios``/``budget_seconds`` are injectable
    for tests; in production they default to the real container transport, the
    pinned judge, the YAML fixtures, and the 240s budget.

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

        t0 = time.monotonic()
        judged_count = 0
        drove_any = False
        first_turn_pending = True  # the run's first driven turn gets the wake-aware deadline

        for index, scenario in enumerate(scenario_list):
            will_judge = (
                bool(scenario.soft_dimensions) and active_judge is not None and judged_count < max_judged_scenarios
            )
            worst_case = _scenario_worst_case_seconds(
                scenario, wake_aware_first_turn=first_turn_pending, will_judge=will_judge
            )
            remaining = budget_seconds - (time.monotonic() - t0)
            if worst_case > remaining:
                # Out of budget: record THIS and every remaining scenario as
                # skipped-with-reason, then stop starting scenarios (time only moves
                # forward, so nothing later can fit either under worst-case gating).
                logger.warning(
                    "behavior: budget exhausted — skipping %d scenario(s) (remaining %.0fs < worst case %.0fs)",
                    len(scenario_list) - index,
                    remaining,
                    worst_case,
                )
                for skipped in scenario_list[index:]:
                    _record_scenario_skipped(run, skipped, "budget")
                break

            # ISOLATION (the run-33 fix): open a FRESH conversation scope so this
            # scenario runs in its own OpenClaw session and cannot see an earlier
            # scenario's transcript. A failure to open raises → record_run closes
            # the run ERROR (INVARIANT #3): we never drive a scenario into a prior
            # scenario's (contaminated) scope and quietly report green.
            active_transport.open_conversation()

            scenario_run = _drive_scenario(scenario, active_transport, wake_aware_first_turn=first_turn_pending)
            first_turn_pending = False
            drove_any = True
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
                            rubric_version="",
                        )
                elif judged_count >= max_judged_scenarios:
                    for dim in scenario.soft_dimensions:
                        _record_soft(
                            run,
                            scenario,
                            dim,
                            score=None,
                            status="skipped",
                            reason="scenario_cap",
                            judge_model="",
                            rubric_version="",
                        )
                else:
                    scores = _safe_judge(active_judge, scenario, scenario_run)
                    judged_count += 1
                    jm = getattr(active_judge, "model", "")
                    for dim in scenario.soft_dimensions:
                        js = scores.get(dim) or JudgeScore(dim, None, ok=False, reason="missing_dimension")
                        if js.ok:
                            _record_soft(
                                run,
                                scenario,
                                dim,
                                score=js.score,
                                status="scored",
                                reason="",
                                judge_model=jm,
                                rubric_version=RUBRIC_VERSION,
                            )
                        else:
                            _record_soft(
                                run,
                                scenario,
                                dim,
                                score=None,
                                status="skipped",
                                reason=js.reason,
                                judge_model=jm,
                                rubric_version="",
                            )

        if not drove_any:
            # Every scenario was budget-skipped before one turn was driven — a run of
            # only skip rows would close PASS while proving NOTHING. Loud, not vacuous
            # (INVARIANT #3): the budget/pack shape is misconfigured.
            raise BehaviorConfigError(
                "behavior budget too small to drive even one scenario — "
                f"budget {budget_seconds:.0f}s cannot fit the first scenario's worst case"
            )

    return run
