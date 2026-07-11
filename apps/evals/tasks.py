"""QStash-dispatched eval tasks (Wave A)."""

from __future__ import annotations


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

    if run.status != EvalRun.Status.PASS:
        raise RuntimeError(f"eval_smoke: run {run.id} closed '{run.status}' — see the eval summary log line")

    return {
        "run_id": run.id,
        "suite": run.suite,
        "status": run.status,
        "cases": run.results.count(),
    }
