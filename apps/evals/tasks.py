"""QStash-dispatched eval tasks (Wave A+)."""

from __future__ import annotations


def finalize_task_run(run) -> None:
    """Shared task-boundary finalizer for every eval task wrapper.

    On a run that did NOT close ``pass``: best-effort alert the platform owner
    (``send_eval_failure_alert`` — never raises), then RAISE ``RuntimeError`` so
    QStash marks the delivery failed and it lands in the DLQ. On a passing run:
    no-op. This is the extraction of the ``eval_smoke_task`` / crypto-smoke
    contract, so Wave B's journey-probe tasks inherit identical fail semantics
    (alert + DLQ, never a silent green).
    """
    from apps.evals.models import EvalRun

    if run.status == EvalRun.Status.PASS:
        return

    from apps.evals.alerting import send_eval_failure_alert

    send_eval_failure_alert(run)  # best-effort; swallows its own errors
    raise RuntimeError(f"eval {run.suite}: run {run.id} closed '{run.status}' — owner alerted, delivery DLQ'd")


def eval_smoke_task() -> dict:
    """Fire the ``eval_smoke`` suite — the chassis proof.

    Zero-arg by contract (the QStash publish path we use can't carry a body),
    registered in apps/cron/views.py TASK_MAP, fired by a no-body publish to
    ``/api/cron/trigger/eval_smoke/``. Writes real EvalRun + EvalResult rows and
    emits the one-line run summary.

    RAISES when the run does not close ``pass`` — so a failing eval lands in the
    QStash DLQ instead of reporting a silent green (the same contract as
    ``crypto_roundtrip_smoke``). Safe to re-fire anytime; each fire is its own run.
    """
    from apps.evals.models import EvalRun
    from apps.evals.suites.smoke import run_smoke_suite

    # Operator-fired today (no schedule exists yet); Wave B flips this to
    # SCHEDULED when a real QStash cron drives it.
    run = run_smoke_suite(trigger=EvalRun.Trigger.MANUAL)

    # Shared contract: non-pass → alert owner + raise into the DLQ; pass → continue.
    finalize_task_run(run)

    return {
        "run_id": run.id,
        "suite": run.suite,
        "status": run.status,
        "cases": run.results.count(),
    }


def eval_journey_wake_task() -> dict:
    """Fire the ``journey_wake`` suite — Probe 4, the hibernation-wake canary.

    Zero-arg by contract (the QStash publish path can't carry a body), registered
    in apps/cron/views.py TASK_MAP, fired by a no-body publish to
    ``/api/cron/trigger/eval_journey_wake/``. Force-hibernates the synthetic
    journey tenant (confirmed via Azure ground truth), then drives one real
    message and asserts the FULL wake chain: waking_at was set (not the warm path)
    AND the turn reached ``ready`` within SLO — see apps/evals/suites/journey_wake.py
    for why each gate is load-bearing.

    RAISES when the run does not close ``pass`` (owner alerted first, then DLQ) —
    the same contract as ``eval_smoke_task``. A ``budget_exhausted`` observation is
    a SOFT pass (the designed cap), so it does NOT raise; a failure to
    ground-truth-hibernate IS a hard FAIL (INVARIANT #3 — never a silent skip).
    Safe to re-fire anytime; each fire is its own run.
    """
    from apps.evals.models import EvalRun
    from apps.evals.suites.journey_wake import run_wake_suite

    # Operator-fired today (no schedule until PR-B6); flips to SCHEDULED when a
    # real QStash cron drives it (staggered off the :00/:30 chat-probe boundary).
    run = run_wake_suite(trigger=EvalRun.Trigger.MANUAL)

    # Shared contract: non-pass → alert owner + raise into the DLQ; pass → continue.
    finalize_task_run(run)

    return {
        "run_id": run.id,
        "suite": run.suite,
        "status": run.status,
        "cases": run.results.count(),
    }
