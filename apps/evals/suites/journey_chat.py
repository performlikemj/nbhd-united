"""Probe 1 — chat round-trip journey canary (docs/evals-wave-b-plan.md).

The single most important probe: it guards the "assistant went silent" incident
class by driving the REAL user path — POST a message as the synthetic journey
tenant, let it flow through ``enqueue_tenant_turn`` -> QStash drain -> the
tenant's OpenClaw container, and poll for the reply — then asserting the turn
actually completed, not merely that a timestamp got stamped.

Why ``replied_at`` is not enough (fact #1): ``replied_at`` is stamped on EVERY
terminal transition, including the failures — ``budget_exhausted`` (at row
creation, chat_views.py), ``empty_response``/``stale``/``dropped``
(pending_queue.py). A probe that asserted "replied_at is set" would read green
on a turn where the assistant said nothing. The real pass predicate is
``status == ready AND error == "" AND source == tenant AND round_trip <= SLO``.

Two green-theater traps this closes (docs/evals-wave-b-plan.md):
  * ``source``: the ``/turns/`` endpoint (ChatLocalTurnView) lets a client record
    its OWN reply as ``source == on_device``. A fabricated reply is ``ready`` with
    ``error == ""`` and instant — it would sail past every check EXCEPT
    ``source == tenant``. So the source assertion is load-bearing, not cosmetic.
  * ``budget_exhausted``: a distinct SOFT outcome — the synthetic tenant self-cap
    (or the global cap) tripping is the DESIGNED safety behavior (fact #3), not a
    broken pipeline. It is recorded under its own case id so it can never be
    mistaken for a proven round-trip, and it does NOT page the owner every 30 min.

INVARIANT #1: this suite records status codes, an error CODE (``budget_exhausted``
is a 64-char machine reason, never content), round-trip ms, and poll counts —
never the reply text or any user data.
"""

from __future__ import annotations

import logging

from apps.evals.journey.chat_drive import (
    DEFAULT_DEADLINE_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SLO_SECONDS,
    ObservedTurn,
    drive_chat_turn,
    resolve_base_url,
    resolve_journey_pat,
)
from apps.evals.journey.targets import resolve_journey_tenant
from apps.evals.models import EvalResult, EvalRun
from apps.evals.runner import record, record_run

logger = logging.getLogger(__name__)

SUITE = "journey_chat"
# The normal round-trip case id: PASS or any HARD failure (pipeline broken,
# wrong source, SLO breach, timeout) is recorded under this id, so a dashboard
# reads "chat round-trip proven / broken" off it directly.
CASE_ROUNDTRIP = "chat_roundtrip"
# Soft budget-cap outcome rides a SEPARATE id so it can never be counted as a
# proven round-trip (it did not run one) — see BUDGET_EXHAUSTED below.
CASE_BUDGET_CAPPED = "chat_roundtrip_budget_capped"

SLO_SECONDS = DEFAULT_SLO_SECONDS
SLO_MS = int(SLO_SECONDS * 1000)

# A minimal, benign prompt. Its content is irrelevant — only the turn's terminal
# metadata is observed — and it is cheap to keep the synthetic tenant's spend low.
PROBE_TEXT = "eval ping — please reply with a short acknowledgement."


class ChatOutcome:
    """Classification of one observed round trip. Distinct, greppable statuses."""

    PASS = "pass"  # ready + tenant + within SLO — the only clean green
    SLO_BREACH = "slo_breach"  # replied correctly but too slow
    WRONG_SOURCE = "wrong_source"  # ready but source != tenant (fabricated/on-device)
    BUDGET_EXHAUSTED = "budget_exhausted"  # SOFT — designed cap tripped, not a break
    PIPELINE_ERROR = "pipeline_error"  # error status (non-budget) or an HTTP failure
    TIMEOUT = "timeout"  # never reached terminal within the deadline (silent)


# Hard failures: recorded ``passed=False`` under CASE_ROUNDTRIP → run FAIL → the
# task-boundary finalizer alerts the owner + DLQs. BUDGET_EXHAUSTED and PASS are
# NOT here (both close the run 'pass' — see run_chat_roundtrip_suite).
_HARD_FAILURES = frozenset(
    {
        ChatOutcome.SLO_BREACH,
        ChatOutcome.WRONG_SOURCE,
        ChatOutcome.PIPELINE_ERROR,
        ChatOutcome.TIMEOUT,
    }
)


def classify_roundtrip(observed: ObservedTurn, *, slo_ms: int = SLO_MS) -> str:
    """Pure classification of an observed turn into a ``ChatOutcome``.

    This is the whole point of the probe, isolated for direct testing. The pass
    predicate is deliberately strict — ``status==ready`` alone is not enough,
    because ``replied_at`` (and thus a naive "did it reply") is stamped on
    failures too (fact #1).
    """
    if not observed.http_ok:
        # The POST or every poll failed at the HTTP layer — the control plane's
        # own API was unreachable/erroring. Pipeline broken.
        return ChatOutcome.PIPELINE_ERROR
    if observed.timed_out or not observed.terminal:
        # Left PENDING past the deadline: the assistant went silent — the exact
        # incident class this probe exists to catch.
        return ChatOutcome.TIMEOUT
    if observed.status == "error":
        # budget_exhausted is the designed cap (fact #3); every other error code
        # (empty_response / stale / dropped / ...) is a real pipeline break.
        if observed.error == "budget_exhausted":
            return ChatOutcome.BUDGET_EXHAUSTED
        return ChatOutcome.PIPELINE_ERROR
    if observed.status == "ready":
        if observed.error != "":
            # Defensive: a 'ready' row must carry no error. An inconsistent pair
            # is a pipeline defect, not a pass.
            return ChatOutcome.PIPELINE_ERROR
        if observed.source != "tenant":
            # Fabricated / on-device reply — the /turns/ trap. Never a pass.
            return ChatOutcome.WRONG_SOURCE
        if observed.round_trip_ms is None:
            # Ready but no created_at/replied_at to time it — malformed. Not a pass.
            return ChatOutcome.PIPELINE_ERROR
        if observed.round_trip_ms > slo_ms:
            return ChatOutcome.SLO_BREACH
        return ChatOutcome.PASS
    # Any other terminal status is unexpected.
    return ChatOutcome.PIPELINE_ERROR


def _details(outcome: str, observed: ObservedTurn) -> dict:
    """Content-free triage sidecar (INVARIANT #1: codes / counts / durations only)."""
    return {
        "outcome": outcome,
        "status": observed.status,
        "source": observed.source,
        "error": observed.error,  # a bounded machine code (<=64 chars), never content
        "round_trip_ms": observed.round_trip_ms,
        "slo_ms": SLO_MS,
        "polls": observed.polls,
        "elapsed_ms": observed.elapsed_ms,
        "http_status": observed.http_status,
        "waking_at": observed.waking_at_seen,  # bool — the wake signal (surfaced for B4)
    }


def run_chat_roundtrip_suite(*, trigger: str = EvalRun.Trigger.MANUAL) -> EvalRun:
    """Drive one real round trip against the synthetic journey tenant; record it.

    Returns the CLOSED run. Resolution (tenant / base URL / PAT) happens INSIDE
    ``record_run`` so a misconfiguration closes the run ``error`` and re-raises
    into the DLQ (docs/evals-directive.md INVARIANT #3 — a probe that cannot run
    FAILS loudly, it never silently passes). ``record_run`` opens no transaction,
    so the httpx round trip is not inside any ``atomic()`` (INVARIANT #8).
    """
    with record_run(SUITE, trigger) as run:  # runtime suite → image_tag auto-infers the fleet tag
        tenant = resolve_journey_tenant()
        base_url = resolve_base_url()
        pat = resolve_journey_pat(tenant)

        observed = drive_chat_turn(
            base_url=base_url,
            pat=pat,
            text=PROBE_TEXT,
            deadline_seconds=DEFAULT_DEADLINE_SECONDS,
            poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
        )
        outcome = classify_roundtrip(observed, slo_ms=SLO_MS)
        details = _details(outcome, observed)

        if outcome == ChatOutcome.BUDGET_EXHAUSTED:
            # SOFT: the designed cap tripped — not a proven round trip, but not a
            # pipeline break either. Recorded passed=True under its OWN case id
            # (never CASE_ROUNDTRIP) with NO round-trip score, so it can't be
            # counted as a real pass and it does not page the owner every 30 min.
            logger.warning("journey_chat: budget_exhausted — synthetic tenant capped, round trip not exercised")
            record(run, CASE_BUDGET_CAPPED, EvalResult.Kind.JOURNEY, passed=True, details=details)
        elif outcome == ChatOutcome.PASS:
            record(
                run,
                CASE_ROUNDTRIP,
                EvalResult.Kind.JOURNEY,
                passed=True,
                score=observed.round_trip_ms,
                threshold=SLO_MS,
                details=details,
            )
        else:
            # Every hard failure (slo_breach / wrong_source / pipeline_error /
            # timeout) → passed=False under CASE_ROUNDTRIP → run FAIL → owner
            # alerted + DLQ. SLO_BREACH keeps its score/threshold for triage.
            is_slo = outcome == ChatOutcome.SLO_BREACH
            record(
                run,
                CASE_ROUNDTRIP,
                EvalResult.Kind.JOURNEY,
                passed=False,
                score=observed.round_trip_ms if is_slo else None,
                threshold=SLO_MS if is_slo else None,
                details=details,
            )
    return run
