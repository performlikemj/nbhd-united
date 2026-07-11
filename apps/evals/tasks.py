"""QStash-dispatched eval tasks (Wave A+)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# A run still ``running`` past this many minutes is provably dead, not slow: every
# probe finishes (or is SIGKILL'd) under the 300s gunicorn worker ceiling
# (config/settings/base.py:327), so 30min sits far above any live run's deadline.
# The reaper only ever catches runs whose worker was killed so hard that
# ``record_run``'s except/finally never ran to close the row — never a live one.
STUCK_RUN_TIMEOUT_MINUTES = 30


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

    # Shared contract: non-pass → alert owner + raise into the DLQ; pass → continue.
    finalize_task_run(run)

    return {
        "run_id": run.id,
        "suite": run.suite,
        "status": run.status,
        "cases": run.results.count(),
    }


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


def eval_journey_cron_task() -> dict:
    """Fire the ``journey_cron`` suite — the cron-fire delivery canary (Probe 3).

    Zero-arg by contract (the QStash publish path can't carry a body), registered
    in apps/cron/views.py TASK_MAP, fired by a no-body publish to
    ``/api/cron/trigger/eval_journey_cron/``. Arms a REAL one-shot ``pure_reminder``
    cron on the synthetic journey tenant, then polls for the ``ProactiveOutbound``
    row the delivery view writes when OpenClaw actually fires it.

    RAISES when the run does not close ``pass`` (via ``finalize_task_run``) — a
    non-delivery lands in the DLQ + alerts the owner instead of a silent green.
    Single-request (arm + observe); see the suite docstring for the two-phase
    fallback if a fire ever brushes the 300s worker ceiling.
    """
    from apps.evals.models import EvalRun
    from apps.evals.suites.journey_cron import run_cron_fire_suite

    # Operator-fired today (no schedule exists yet); Wave B6 flips this to
    # SCHEDULED when a real QStash cron drives it.
    run = run_cron_fire_suite(trigger=EvalRun.Trigger.MANUAL)

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


def reap_stuck_eval_runs_task() -> dict:
    """Flip orphaned ``running`` eval runs to ``error`` — the crash-recovery sweep.

    Zero-arg by contract (the QStash publish path we use can't carry a body),
    registered in apps/cron/views.py TASK_MAP, fired by a no-body publish to
    ``/api/cron/trigger/reap_stuck_eval_runs/``.

    ``record_run`` closes every run it opens, even on an exception — EXCEPT when
    the worker is SIGKILL'd (e.g. the 300s gunicorn timeout, config/settings/
    base.py:327) so hard that its ``except``/``finally`` never runs, stranding an
    ``EvalRun`` at ``status='running'`` forever. This sweep is the backstop: any
    run still ``running`` past ``STUCK_RUN_TIMEOUT_MINUTES`` is flipped to
    ``error`` with ``finished_at`` stamped.

    The 30-min floor is the whole safety property: it sits far above every probe's
    sub-300s deadline, so this only ever reaps a truly-dead run — a run legitimately
    in flight is always younger than the cutoff and is left strictly untouched.

    Returns the reaped count + ids. Does NOT raise on a successful reap — reaping is
    the reaper doing its job, not an eval failure (only its own crash should DLQ).
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.evals.models import EvalRun

    cutoff = timezone.now() - timedelta(minutes=STUCK_RUN_TIMEOUT_MINUTES)
    stranded = list(EvalRun.objects.filter(status=EvalRun.Status.RUNNING, started_at__lt=cutoff))

    if not stranded:
        logger.info(
            "eval reaper: 0 reaped — no run has been 'running' longer than %dm",
            STUCK_RUN_TIMEOUT_MINUTES,
        )
        return {"reaped": 0, "run_ids": []}

    reaped_ids = [run.id for run in stranded]
    finished = timezone.now()
    # Atomic flip. Re-assert status='running' in the filter so a run that somehow
    # closed legitimately between the select above and this update is left alone
    # (belt-and-suspenders; a >30m 'running' run is already provably dead).
    EvalRun.objects.filter(id__in=reaped_ids, status=EvalRun.Status.RUNNING).update(
        status=EvalRun.Status.ERROR, finished_at=finished
    )
    logger.error(
        "eval reaper: flipped %d orphaned eval run(s) running->error (worker likely SIGKILL'd): %s",
        len(reaped_ids),
        reaped_ids,
    )

    # Best-effort, content-free owner alert per reaped run — a stranded run means a
    # probe worker died mid-flight, worth surfacing. send_eval_failure_alert never
    # raises and is gated on PLATFORM_OWNER_EMAIL; the loop stays defensive anyway
    # so alerting can never break the reap it follows.
    from apps.evals.alerting import send_eval_failure_alert

    for run in stranded:
        # Mutate the in-memory copy so the alert reflects the reaped state (the
        # bulk update above already persisted it; we don't re-save here).
        run.status = EvalRun.Status.ERROR
        run.finished_at = finished
        try:
            send_eval_failure_alert(run)
        except Exception:  # pragma: no cover — the helper is documented never to raise
            logger.exception("eval reaper: failure alert errored for reaped run %s", run.id)

    return {"reaped": len(reaped_ids), "run_ids": reaped_ids}
