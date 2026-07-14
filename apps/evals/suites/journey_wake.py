"""Probe 4 — hibernation-wake journey canary (docs/evals-wave-b-plan.md).

The historically fragile path: a hibernated tenant (0 replicas, 0 cost) must wake
and reply when a message arrives. Cold start "regularly runs past 2 minutes and
has hit 3 in the worst case" (hibernation.py:29-41), and the wake chain has
produced silent-loop incidents (canary 148ccf1c). This probe drives the REAL
path and asserts the WHOLE chain actually ran.

DO NOT DISARM THIS WITHOUT READING THIS PARAGRAPH. Since 2026-07-14, Suite 4's
``compute_reply_latency`` deliberately EXCLUDES turns that woke a container (they were
being judged twice, against two ceilings that disagree — see slo_snapshot.py), and
``compute_wake_latency_p95`` measures only the WAKE PORTION (``waking_at → replied_at``),
not the full wait. So THIS CANARY is the only end-to-end coverage of what a real user
actually experiences on a cold start. Suite 4's deferral of that metric is honest ONLY
while this probe runs. Disarm it, let its tenant's budget cap trip for a stretch, or
deprovision that tenant, and the cold-start hole silently reopens — with Suite 4 still
reporting green, because it deliberately isn't looking.

It reuses Probe 1's driver (``apps/evals/journey/chat_drive.drive_chat_turn``) —
same POST-a-message-and-poll implementation — so the wake probe and the chat
probe share one real-path core. What makes THIS the wake probe is the three hard
gates, each of which closes a specific way a wake probe reads green while proving
nothing (docs/evals-wave-b-plan.md Probe 4):

  Gate 1 — GROUND-TRUTH HIBERNATED before sending. ``force_hibernate_and_confirm``
    hibernates the tenant and confirms via Azure (0 active revisions), not the DB
    flag (which drifts both ways). If it can't hibernate after a bounded retry —
    something keeps waking it — that is a real FAIL, never a skip (INVARIANT #3:
    the chassis has no skip state; a precondition failure is a recorded failure).

  Gate 2 — ``waking_at`` was POSITIVELY set. THE assertion. ``_mark_ios_waking``
    stamps ``waking_at`` only on the wake branch of the drain; a warm turn leaves
    it null. A fast ``ready`` reply with ``waking_at`` null means the WARM path
    ran on a tenant that was never actually asleep — the probe would pass without
    ever exercising a wake. So ``waking_at_seen`` must be True.

  Gate 3 — terminal ``status == "ready"`` SPECIFICALLY. A stuck turn flips to
    ERROR (pending_queue.py:1241-1243); a silent one never leaves PENDING. "Left
    PENDING" or "terminal error" is a FAIL — proving the wake reached a real
    reply, not merely that the container stirred.

Plus the round-trip SLO (~180s; deadline ~240s catches the worst-case cold start
while staying under the 300s worker ceiling) and a cross-check that the wake
cleared ``hibernated_at`` and stamped ``last_wake_at`` (hibernation.py:530).

B6 SCHEDULING NOTE: staggering off the 30-min chat probe's :00/:30 boundary is
NOT sufficient. ``hibernate_idle_tenant`` itself arms a QStash cron-wake at
``nextRun − 240s`` for the synthetic tenant's OWN OpenClaw crons
(hibernation.py:116 → ``_schedule_next_cron_wake``). If the daily wake schedule
lands near one of those OC cron-wake fire times, that armed wake can wake the
tenant mid-test — a residual Probe-1↔Probe-4-class race. So B6 must also stagger
the wake schedule away from the synthetic tenant's own cron-wake times (the
ground-truth precondition catches whatever races through, as a clean FAIL).

INVARIANT #1: this suite records status codes, machine error codes, booleans,
counts and durations — never the reply text, user text, or any user data.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from apps.evals.journey.chat_drive import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    ObservedTurn,
    drive_chat_turn,
    resolve_base_url,
    resolve_journey_pat,
)
from apps.evals.journey.targets import resolve_journey_tenant
from apps.evals.journey.wake_control import HibernateResult, force_hibernate_and_confirm
from apps.evals.models import EvalResult, EvalRun
from apps.evals.runner import record, record_run

logger = logging.getLogger(__name__)

SUITE = "journey_wake"

# Case ids — a dashboard reads each concern off its own id.
CASE_HIBERNATED = "wake_precondition_hibernated"  # Gate 1: ground-truth hibernated
CASE_WAKE = "wake_roundtrip"  # Gates 2+3 + SLO: the wake actually completed
CASE_WAKE_BUDGET_CAPPED = "wake_roundtrip_budget_capped"  # SOFT: designed cap, no wake exercised
CASE_FLAGS = "wake_flags_cross_check"  # secondary: hibernated_at cleared + last_wake_at recent

# Wake SLO. Cold start can hit ~3 min; the SLO is 180s and the poll deadline 240s
# (60s headroom) — both under the 300s gunicorn worker ceiling once the
# force-hibernate precondition (~18s worst case) is added (docs/evals-wave-b-plan.md
# Probe 4 + fact #2). Two-phase fallback is the escape hatch if cold starts brush
# the ceiling.
WAKE_SLO_SECONDS = 180
WAKE_DEADLINE_SECONDS = 240
SLO_MS = int(WAKE_SLO_SECONDS * 1000)

# Slack for the last_wake_at cross-check: a real wake stamps last_wake_at at drain
# time (before the container even boots), so by the time the turn is ready it is
# well after t0. The slack tolerates clock skew and a near-simultaneous wake from
# the 30-min chat probe (docs/evals-wave-b-plan.md Probe-1↔Probe-4 race) while
# still catching a stale last_wake_at from hours ago.
_LAST_WAKE_SLACK_SECONDS = 90

# Benign, cheap probe prompt. Only the turn's terminal metadata is observed; the
# text is irrelevant (and kept minimal to keep the synthetic tenant's spend low).
PROBE_TEXT = "eval wake ping — please reply with a short acknowledgement."


class WakeOutcome:
    """Classification of one observed wake attempt. Distinct, greppable statuses."""

    PASS = "pass"  # hibernated → waking_at seen → ready within SLO — the only clean green
    WARM_PATH = "warm_path"  # ready but waking_at never set — the tenant wasn't asleep (Gate 2)
    SLO_BREACH = "slo_breach"  # woke + ready but slower than the SLO
    WRONG_SOURCE = "wrong_source"  # ready but source != tenant (fabricated /turns/ reply)
    BUDGET_EXHAUSTED = "budget_exhausted"  # SOFT — designed cap tripped pre-turn, no wake exercised
    PIPELINE_ERROR = "pipeline_error"  # terminal error (non-budget), HTTP failure, or malformed row
    TIMEOUT = "timeout"  # never reached terminal within the deadline (silent after wake) (Gate 3)


def classify_wake(observed: ObservedTurn, *, slo_ms: int = SLO_MS) -> str:
    """Pure classification of an observed wake turn into a ``WakeOutcome``.

    The whole point of the probe, isolated for direct testing. The order is
    load-bearing:

    * budget_exhausted is checked BEFORE the waking_at gate — a budget-capped turn
      never woke the container (fact #3: the cap trips pre-turn), so ``waking_at``
      null is EXPECTED there, not a warm-path bug. Misordering would misclassify a
      designed cap as a hard WARM_PATH failure and page the owner.
    * Gate 2 (``waking_at_seen``) is checked only on a clean ``ready`` turn — a
      ready reply with waking_at null is the warm path, the #1 green-theater trap.
    """
    if not observed.http_ok:
        # POST or every poll failed at the HTTP layer — control plane unreachable.
        return WakeOutcome.PIPELINE_ERROR
    if observed.status == "error" and observed.error == "budget_exhausted":
        # SOFT: the designed spend cap tripped before any container work. The wake
        # path was not exercised this run (waking_at null is expected).
        return WakeOutcome.BUDGET_EXHAUSTED
    if observed.timed_out or not observed.terminal:
        # Left PENDING past the deadline — the assistant went silent after the
        # wake attempt (Gate 3: not "left PENDING", a real terminal reply).
        return WakeOutcome.TIMEOUT
    if observed.status == "error":
        # Any non-budget terminal error (empty_response / stale / dropped / a
        # stuck turn flipped to ERROR) is a real pipeline break — even if
        # waking_at was seen (Gate 3: the wake must reach ``ready``, not error).
        return WakeOutcome.PIPELINE_ERROR
    if observed.status == "ready":
        if observed.error != "":
            # A 'ready' row must carry no error; an inconsistent pair is a defect.
            return WakeOutcome.PIPELINE_ERROR
        if observed.source != "tenant":
            # Fabricated / on-device reply — never a proven wake.
            return WakeOutcome.WRONG_SOURCE
        if not observed.waking_at_seen:
            # GATE 2 — THE assertion. Ready, from the tenant, no error — but
            # waking_at was never set, so the WARM path ran on a tenant that was
            # not actually asleep. The wake path was NOT exercised. Not a pass.
            return WakeOutcome.WARM_PATH
        if observed.round_trip_ms is None:
            # Ready but no created_at/replied_at to time it — malformed. Not a pass.
            return WakeOutcome.PIPELINE_ERROR
        if observed.round_trip_ms > slo_ms:
            return WakeOutcome.SLO_BREACH
        return WakeOutcome.PASS
    # Any other terminal status is unexpected.
    return WakeOutcome.PIPELINE_ERROR


def _details(outcome: str, observed: ObservedTurn, hib: HibernateResult) -> dict:
    """Content-free triage sidecar (INVARIANT #1: codes / counts / booleans only)."""
    return {
        "outcome": outcome,
        "status": observed.status,
        "source": observed.source,
        "error": observed.error,  # a bounded machine code (<=64 chars), never content
        "waking_at_seen": observed.waking_at_seen,  # Gate 2 signal
        "phase_seen": observed.phase_seen,  # container-emitted liveness (advisory)
        "round_trip_ms": observed.round_trip_ms,
        "slo_ms": SLO_MS,
        "polls": observed.polls,
        "elapsed_ms": observed.elapsed_ms,
        "http_status": observed.http_status,
        "hibernate_attempts": hib.attempts,  # Gate 1 precondition metadata
        "hibernated_confirmed": hib.hibernated,
    }


def run_wake_suite(*, trigger: str = EvalRun.Trigger.MANUAL) -> EvalRun:
    """Force-hibernate the synthetic journey tenant, drive one wake, record it.

    Returns the CLOSED run. Resolution + force-hibernate + the httpx round trip all
    run INSIDE ``record_run`` so a misconfiguration or a precondition failure
    closes the run loudly (``error`` on a raise, ``fail`` on a recorded failing
    case) into the DLQ — never a silent pass (INVARIANT #3). ``record_run`` opens
    no transaction, so no external call is inside an ``atomic()`` (INVARIANT #8).
    """
    with record_run(SUITE, trigger) as run:  # runtime suite → image_tag auto-infers the fleet tag
        tenant = resolve_journey_tenant()
        base_url = resolve_base_url()
        pat = resolve_journey_pat(tenant)

        # GATE 1 — ground-truth hibernate BEFORE sending. Requires BOTH Azure
        # confirmation (0 active revisions) AND the DB flag stamped. The flag is
        # load-bearing, not just a datapoint: if hibernate_idle_tenant's Azure
        # deactivate succeeds server-side but the SDK call raises client-side
        # (timeout), it returns without stamping ``hibernated_at`` — the container
        # is DOWN but the flag is null. Gating on Azure alone would "pass" here,
        # then the driven message 404s and the drain's wake branch (which fires
        # only when hibernated_at is non-null) NEVER runs → misattributed as
        # "wake broken" AND the tenant is left BRICKED (down + flag null) so every
        # subsequent chat probe also drops until the next force-hibernate restamps.
        # Requiring flag_set turns that into a clean, paged Gate-1 FAIL instead.
        hib = force_hibernate_and_confirm(tenant)
        if not (hib.hibernated and hib.flag_set):
            # Precondition failed — the tenant would not stay asleep, or hibernated
            # inconsistently (Azure down but flag null). A real FAIL, never a skip:
            # driving a message now would exercise the WARM path (or a bricked
            # container) and prove nothing (INVARIANT #3, docs/evals-wave-b-plan.md
            # Gate 1).
            logger.error(
                "journey_wake: could not ground-truth hibernate (attempts=%d, confirmed=%s, flag=%s, stage=%s) — "
                "precondition FAIL",
                hib.attempts,
                hib.hibernated,
                hib.flag_set,
                hib.failure_stage,
            )
            record(
                run,
                CASE_HIBERNATED,
                EvalResult.Kind.JOURNEY,
                passed=False,
                details={
                    "hibernated_confirmed": hib.hibernated,
                    "hibernate_attempts": hib.attempts,
                    "flag_set": hib.flag_set,
                    "failure_stage": hib.failure_stage,
                },
            )
            return run  # record_run closes it FAIL (a failing case), then DLQs via the task wrapper

        record(
            run,
            CASE_HIBERNATED,
            EvalResult.Kind.JOURNEY,
            passed=True,
            details={"hibernated_confirmed": True, "flag_set": True, "hibernate_attempts": hib.attempts},
        )

        # Drive the wake on the REAL path (Probe 1's driver), timing the whole
        # cold start. t0 anchors the last_wake_at cross-check below.
        t0 = timezone.now()
        observed = drive_chat_turn(
            base_url=base_url,
            pat=pat,
            text=PROBE_TEXT,
            deadline_seconds=WAKE_DEADLINE_SECONDS,
            poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
        )
        outcome = classify_wake(observed, slo_ms=SLO_MS)
        details = _details(outcome, observed, hib)

        if outcome == WakeOutcome.BUDGET_EXHAUSTED:
            # SOFT: the designed spend cap tripped pre-turn (fact #3); the wake was
            # not exercised. Recorded passed=True under its OWN case id (never
            # CASE_WAKE) with no score, so it can't be counted as a proven wake and
            # it does not page the owner. The flag cross-check is skipped (the
            # tenant is still — correctly — hibernated).
            logger.warning("journey_wake: budget_exhausted — synthetic tenant capped, wake not exercised")
            record(run, CASE_WAKE_BUDGET_CAPPED, EvalResult.Kind.JOURNEY, passed=True, details=details)
            return run

        if outcome == WakeOutcome.PASS:
            record(
                run,
                CASE_WAKE,
                EvalResult.Kind.JOURNEY,
                passed=True,
                score=observed.round_trip_ms,
                threshold=SLO_MS,
                details=details,
            )
        else:
            # Every hard failure (warm_path / slo_breach / wrong_source /
            # pipeline_error / timeout) → passed=False under CASE_WAKE → run FAIL →
            # owner alerted + DLQ. SLO_BREACH keeps its score/threshold for triage.
            is_slo = outcome == WakeOutcome.SLO_BREACH
            record(
                run,
                CASE_WAKE,
                EvalResult.Kind.JOURNEY,
                passed=False,
                score=observed.round_trip_ms if is_slo else None,
                threshold=SLO_MS if is_slo else None,
                details=details,
            )

        # Secondary cross-check: a real wake clears hibernated_at and stamps
        # last_wake_at (hibernation.py:530). Both are deterministic post-wake, so a
        # ready-but-still-flagged row is a real inconsistency. Only meaningful once
        # a wake was actually driven (skipped for the budget soft-pass above).
        tenant.refresh_from_db(fields=["hibernated_at", "last_wake_at"])
        cleared = tenant.hibernated_at is None
        last_wake = tenant.last_wake_at
        woke_recently = last_wake is not None and last_wake >= t0 - timedelta(seconds=_LAST_WAKE_SLACK_SECONDS)
        record(
            run,
            CASE_FLAGS,
            EvalResult.Kind.JOURNEY,
            passed=bool(cleared and woke_recently),
            details={"hibernated_at_cleared": cleared, "last_wake_recent": woke_recently},
        )
    return run
