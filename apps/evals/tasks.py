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


def eval_journey_chat_task() -> dict:
    """Fire the ``journey_chat`` suite — Probe 1, the chat round-trip canary.

    Zero-arg by contract (the QStash publish path can't carry a body), registered
    in apps/cron/views.py TASK_MAP, fired by a no-body publish to
    ``/api/cron/trigger/eval_journey_chat/``. Drives one real turn against the
    synthetic journey tenant (message → drain → container → reply) and records
    whether the round trip actually completed within SLO — see
    apps/evals/suites/journey_chat.py for why ``replied_at`` alone is insufficient.

    RAISES when the run does not close ``pass`` (owner alerted first, then DLQ) —
    the same contract as ``eval_smoke_task``. A ``budget_exhausted`` observation
    is a SOFT pass (the designed cap), so it does NOT raise. Safe to re-fire
    anytime; each fire is its own run with a fresh ``client_msg_id``.
    """
    from apps.evals.models import EvalRun
    from apps.evals.suites.journey_chat import run_chat_roundtrip_suite

    # Operator-fired today (no schedule until PR-B6); flips to SCHEDULED when a
    # real QStash cron drives it.
    run = run_chat_roundtrip_suite(trigger=EvalRun.Trigger.MANUAL)
def eval_journey_journal_task(transport=None) -> dict:
    """Fire the ``journey_journal`` probe — journal write→search (Wave B, Probe 2).

    Drives the real ``RuntimeDocumentView`` write + real Postgres-FTS
    ``RuntimeJournalSearchView`` read against the synthetic journey tenant
    (resolved by ``EVAL_JOURNEY_TENANT_ID``). Registered in
    apps/cron/views.py TASK_MAP; operator-fired today via a no-body publish to
    ``/api/cron/trigger/eval_journey_journal/`` (a schedule is added later in
    PR-B6). Writes real EvalRun/EvalResult rows and emits the one-line summary.

    RAISES when the run does not close ``pass`` (broken write/search path, or a
    misconfigured/missing synthetic tenant) — alerting the owner and landing the
    delivery in the QStash DLQ instead of a silent green.

    ``transport`` stays ``None`` in production (real HTTP via
    ``HttpxRuntimeTransport``); tests inject an in-process transport that drives
    the identical endpoints. The default keeps the QStash zero-arg contract.
    """
    from apps.evals.models import EvalRun
    from apps.evals.suites.journey_journal import run_journal_search_suite

    # Operator-fired today (no schedule yet); PR-B6 flips this to SCHEDULED.
    run = run_journal_search_suite(transport=transport, trigger=EvalRun.Trigger.MANUAL)

    # Shared contract: non-pass → alert owner + raise into the DLQ; pass → continue.
    finalize_task_run(run)

    return {
        "run_id": run.id,
        "suite": run.suite,
        "status": run.status,
        "cases": run.results.count(),
    }
