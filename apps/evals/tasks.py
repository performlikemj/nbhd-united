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

    # Fired by the eval-journey-chat QStash cron (PR-B6, */30) — a scheduled run.
    run = run_chat_roundtrip_suite(trigger=EvalRun.Trigger.SCHEDULED)

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

    # Fired by the eval-journey-journal QStash cron (PR-B6, daily 05:05) — scheduled.
    run = run_journal_search_suite(transport=transport, trigger=EvalRun.Trigger.SCHEDULED)

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

    # Fired by the eval-journey-cron QStash cron (PR-B6, daily 05:20) — a scheduled run.
    run = run_cron_fire_suite(trigger=EvalRun.Trigger.SCHEDULED)

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

    # Fired by the eval-journey-wake QStash cron (PR-B6, daily 05:12, staggered off
    # the :00/:30 chat-probe boundary) — a scheduled run.
    run = run_wake_suite(trigger=EvalRun.Trigger.SCHEDULED)

    # Shared contract: non-pass → alert owner + raise into the DLQ; pass → continue.
    finalize_task_run(run)

    return {
        "run_id": run.id,
        "suite": run.suite,
        "status": run.status,
        "cases": run.results.count(),
    }


def eval_behavior_task(transport=None, judge=None) -> dict:
    """Fire the ``behavior`` suite — Wave D model-behavior evals (INERT).

    Zero-arg by contract for the QStash publish path (registered in
    apps/cron/views.py TASK_MAP, fired by a no-body publish to
    ``/api/cron/trigger/eval_behavior/``). Drives the YAML scenario fixtures against
    the synthetic behavior tenant's container, checks deterministic hard assertions
    (which GATE the run), and scores soft dimensions with the pinned judge (which are
    ADVISORY / non-gating).

    RAISES when the run does not close ``pass`` — the same contract as
    ``eval_smoke_task``, on BOTH failure shapes:
      * a driven run that closes FAIL → ``finalize_task_run`` alerts the owner then
        raises into the DLQ;
      * a config/setup exception (e.g. tenant unset while this lands INERT) →
        ``record_run`` already closed the row ``error``; this wrapper best-effort
        alerts the owner on that errored run BEFORE re-raising, so "DLQ + owner
        email" is true on the config path too (the exception would otherwise skip
        ``finalize_task_run`` and no email would go out).
    A fire in prod today (tenant unprovisioned) takes the second path: run closes
    ``error`` → owner email + DLQ — the correct loud signal, not a silent green.
    Fire-verification follows provisioning.

    ``transport``/``judge`` stay ``None`` in production (real container transport +
    the default OpenRouter judge). Tests inject in-process fakes; the defaults keep
    the QStash zero-arg contract intact.
    """
    from apps.evals.models import EvalRun
    from apps.evals.suites.behavior import SUITE, run_behavior_suite

    # SCHEDULED since #1178 armed the nightly QStash cron (05:40 UTC, 2026-07-13).
    # This said MANUAL until 2026-07-14, so every scheduled run was mislabelled in the
    # DB and an operator fire was indistinguishable from a nightly one — which matters
    # the moment anyone trends pass-rate by trigger.
    kwargs: dict = {"transport": transport, "trigger": EvalRun.Trigger.SCHEDULED}
    # judge is tri-state in the suite (unset → default judge). The task can't carry a
    # judge over QStash, so pass it through only when a test injects one; otherwise
    # let the suite build the default (or record skipped-with-reason if unconfigured).
    if judge is not None:
        kwargs["judge"] = judge
    try:
        run = run_behavior_suite(**kwargs)
    except Exception:
        # record_run closed the run 'error' before re-raising — alert the owner on
        # it (best-effort; the helper never raises) so a misconfiguration emails AND
        # DLQs instead of DLQ-only. Newest errored run for this suite is the one the
        # context manager just closed.
        from apps.evals.alerting import send_eval_failure_alert

        errored = EvalRun.objects.filter(suite=SUITE, status=EvalRun.Status.ERROR).order_by("-started_at").first()
        if errored is not None:
            send_eval_failure_alert(errored)
        raise

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


def slo_snapshot_task() -> dict:
    """Fire the ``slo_snapshot`` suite — the nightly production-SLO readout (Suite 4).

    Zero-arg by contract (the QStash publish path can't carry a body), registered
    in apps/cron/views.py TASK_MAP and fired via a no-body QStash delivery to
    ``/api/cron/trigger/slo_snapshot/``. Computes metadata-only SLO metrics over
    the last 24h (reply/wake latency percentiles, error-status rate,
    proactive-delivery volume — ALL ProactiveOutbound producers, not cron health;
    see the suite's named deferrals — stranded/error EvalRun count, and
    journey-canary budget-cap saturation; synthetic tenants excluded, no message
    content read) and records one EvalResult per metric through the chassis.

    A threshold breach → the run closes ``fail`` → ``finalize_task_run`` alerts the
    owner + RAISES into the DLQ — that IS the "breach flagged" mechanism (there is
    no separate alarm path). A compute crash closes the run ``error`` inside
    ``record_run``; this wrapper alerts on that error row before re-raising, so a
    snapshot that could not run is visible through both owner email and QStash.
    Safe to re-fire anytime — each fire is its own run. See
    apps/evals/suites/slo_snapshot.py.
    """
    from apps.evals.models import EvalRun
    from apps.evals.suites.slo_snapshot import SUITE, run_slo_snapshot_suite

    # SCHEDULED since #1178 armed the nightly QStash cron (05:55 UTC, 2026-07-13) —
    # this is the "later PR" the old comment promised. It said MANUAL until
    # 2026-07-14, so every nightly snapshot was mislabelled as an operator fire.
    try:
        run = run_slo_snapshot_suite(trigger=EvalRun.Trigger.SCHEDULED)
    except Exception:
        from apps.evals.alerting import send_eval_failure_alert

        errored = EvalRun.objects.filter(suite=SUITE, status=EvalRun.Status.ERROR).order_by("-started_at").first()
        if errored is not None:
            send_eval_failure_alert(errored)
        raise

    # Shared contract: a breached (non-pass) run → alert owner + raise into the DLQ.
    finalize_task_run(run)

    return {
        "run_id": run.id,
        "suite": run.suite,
        "status": run.status,
        "cases": run.results.count(),
    }


def weekly_slo_digest_task() -> dict:
    """Email the platform owner the trailing-7-day SLO digest (Suite 4, Monday).

    Zero-arg by contract, registered in apps/cron/views.py TASK_MAP and fired via a
    no-body delivery to ``/api/cron/trigger/weekly_slo_digest/``. Reads the
    week's ``slo_snapshot`` EvalRun/EvalResult rows, renders a one-page plain-text
    trend (per-metric min/max/latest vs threshold + breach days), and sends it via
    the gated ``send_slo_digest``.

    Sent and no-owner outcomes return normally; a missing owner is legitimately a
    quiet skip because there is no configured recipient. An attempted send that
    fails raises at this task boundary so the cron endpoint returns non-2xx and
    QStash retries/records the failed delivery. See
    apps/evals/suites/slo_snapshot.py::build_weekly_digest.
    """
    from apps.evals.alerting import send_slo_digest
    from apps.evals.suites.slo_snapshot import build_weekly_digest

    subject, body = build_weekly_digest()
    outcome = send_slo_digest(subject, body)
    if outcome == "sent":
        logger.info("slo digest: weekly readout sent")
        return {"sent": True}
    if outcome == "skipped_no_owner":
        logger.info("slo digest: weekly readout skipped (owner email unset)")
        return {"sent": False, "reason": "no_owner"}
    if outcome == "failed":
        raise RuntimeError("slo digest: weekly readout delivery failed")
    raise RuntimeError(f"slo digest: unknown delivery outcome {outcome!r}")
