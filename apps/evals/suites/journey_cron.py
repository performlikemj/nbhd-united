"""``journey_cron`` — cron-fire delivery canary (Wave B / Probe 3).

Proves the whole proactive-cron pipeline is live end-to-end: a one-shot typed
cron is created on the SYNTHETIC journey tenant, OpenClaw's INTERNAL scheduler
fires it, the fire runs an agent turn that calls ``nbhd_send_to_user``, that hits
``CronDeliveryView`` which writes a ``ProactiveOutbound`` row. The probe drives
the REAL production reminder path (``create_typed_cron`` → gateway ``cron.add``);
it never fabricates a delivery.

WHY THE ASSERTION IS ``ProactiveOutbound``, NOT ``CronJob`` (green-theater guard):
``CronJob.enabled`` / ``last_synced_at`` / ``last_pushed_to_container_at`` only
prove the job was REGISTERED into OpenClaw's mirror — there is deliberately no
``last_fired`` column. A cron can register and never fire (container asleep,
scheduler wedged, agent turn errored). The ONLY server-side evidence that the
cron actually FIRED and DELIVERED is a fresh ``ProactiveOutbound`` row written by
the delivery view. So the pass condition is exactly:

    ProactiveOutbound.objects.filter(
        tenant=<synthetic>, job_name=<this run's unique name>,
        created_at__gte=<window opened before arming>,
    ).exists()

Double-scoped so a stale historical row can never read green forever: the
``job_name`` is unique per run AND ``created_at`` must fall inside the window
opened just before this run armed its cron.

Single-phase (arm + observe in ONE run): the cron is scheduled ``lead_seconds``
out and the run polls until a wall-clock BUDGET measured from SUITE START (t0)
elapses. Anchoring the deadline at t0 (not at the start of polling) is
load-bearing: ``invoke_gateway_tool`` can burn ~90s (45s x2 retries), so a poll
window measured post-arm could push total wall-clock past the 300s gunicorn
worker ceiling (``config/settings/base.py`` / ``startup.sh``) → the worker is
SIGKILL'd mid-poll → a stranded ``running`` row (messier than a clean FAIL +
owner email). Bounding arm + poll from t0 keeps the whole suite under the ceiling
regardless of how long arming took. If prod fire-verification ever shows even
that brushing the ceiling, the documented fallback is two-phase — arm run N,
observe run N+1 — but the primary design here is single-phase because it gives a
true same-run end-to-end assertion.

INVARIANT #1: no real-user content is involved. The fixed synthetic reminder is
stored on the sink's ProactiveOutbound evidence row; the EvalResult records only
existence, counts, and durations, never the body.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import namedtuple
from datetime import timedelta

from django.utils import timezone

from apps.evals.journey.targets import resolve_journey_tenant
from apps.evals.models import EvalResult, EvalRun
from apps.evals.runner import record, record_run

logger = logging.getLogger(__name__)

SUITE = "journey_cron"
CASE_ID = "cron_fire_delivery"

# Timing budget (all bounded under the 300s worker ceiling — see module docstring).
# ``lead_seconds`` mirrors the plan's "60-90s out". ``POLL_BUDGET_SECONDS`` is the
# TOTAL wall-clock budget measured from suite start (t0): arm + poll must finish
# within it, so a slow ~90s arm can never combine with a full poll to exceed 300s.
SCHEDULE_LEAD_SECONDS = 75
POLL_BUDGET_SECONDS = 240
POLL_INTERVAL_SECONDS = 5

# Fixed synthetic reminder text (no PII). Sent through ``nbhd_send_to_user`` and
# stored on the eval-only ProactiveOutbound evidence row; no user transport sees it.
_REMINDER_TEXT = "eval-journey cron-fire canary — automated probe, please disregard."

DeliveryObservation = namedtuple("DeliveryObservation", ["delivered", "poll_count", "elapsed_ms"])


def _unique_job_name() -> str:
    """A per-run cron name unique enough that no prior run's row can collide.

    Kept short (<= the 64-char ``ProactiveOutbound.job_name`` / ``X-NBHD-Job-Name``
    ceiling) so the exact-match filter in ``_observe_delivery`` lines up with the
    truncated value the delivery view stores.
    """
    return f"eval-cron-{secrets.token_hex(8)}"  # 10 + 16 = 26 chars


def _arm_one_shot_cron(tenant, *, name: str, lead_seconds: int):
    """Create a REAL one-shot ``pure_reminder`` cron ``lead_seconds`` in the future.

    Goes through the production ``create_typed_cron`` service, which pushes the
    at-kind job to OpenClaw immediately (``managed=False``, OC auto-deletes it
    after it fires). ``pure_reminder`` is chosen because its fire is a single
    ``nbhd_send_to_user`` of verbatim text — the most deterministic delivery
    pattern, so a non-delivery is a real pipeline failure, not model flakiness.
    """
    from apps.cron.models import CronPattern
    from apps.cron.services import create_typed_cron

    fire_at = (timezone.now() + timedelta(seconds=lead_seconds)).isoformat()
    return create_typed_cron(
        tenant=tenant,
        pattern=CronPattern.PURE_REMINDER,
        typed_payload={"text": _REMINDER_TEXT},
        name=name,
        schedule={"kind": "at", "at": fire_at},
    )


def _observe_delivery(
    tenant,
    *,
    job_name: str,
    window_start,
    deadline: float,
    interval_seconds: float,
    sleep_fn=time.sleep,
) -> DeliveryObservation:
    """Poll for the ONE piece of evidence a cron fired: a fresh ProactiveOutbound.

    ``deadline`` is an ABSOLUTE ``time.monotonic()`` value — the caller anchors it
    at suite start so total arm+poll wall-clock stays bounded (see module
    docstring). Returns metadata only (delivered flag + poll count + elapsed ms) —
    never any row content. The filter is the whole point of the probe: unique
    ``job_name`` AND ``created_at__gte=window_start`` so neither a stale historical
    row nor an unrelated concurrent cron's row can satisfy it.
    """
    from apps.router.models import ProactiveOutbound

    start = time.monotonic()
    poll_count = 0
    delivered = False
    while True:
        poll_count += 1
        delivered = ProactiveOutbound.objects.filter(
            tenant=tenant,
            channel=ProactiveOutbound.Channel.EVAL,
            job_name=job_name,
            created_at__gte=window_start,
        ).exists()
        if delivered or time.monotonic() >= deadline:
            break
        sleep_fn(interval_seconds)

    return DeliveryObservation(
        delivered=delivered,
        poll_count=poll_count,
        elapsed_ms=int((time.monotonic() - start) * 1000),
    )


def run_cron_fire_suite(
    *,
    trigger: str = EvalRun.Trigger.MANUAL,
    lead_seconds: int = SCHEDULE_LEAD_SECONDS,
    budget_seconds: float = POLL_BUDGET_SECONDS,
    interval_seconds: float = POLL_INTERVAL_SECONDS,
    sleep_fn=time.sleep,
) -> EvalRun:
    """Arm a one-shot cron on the journey tenant and assert it actually delivered.

    Uses ``record_run`` so a crash mid-suite closes the run ``error`` (never a
    stranded ``running``). A misconfigured target (``resolve_journey_tenant``
    raises) closes ``error`` and lands in the DLQ — a probe that cannot run FAILS
    loudly, it never silently passes (directive INVARIANT #3).
    """
    from apps.cron.gateway_client import GatewayError
    from apps.cron.services import TypedCronError

    # Anchor the poll deadline to SUITE START (t0), BEFORE arming: invoke_gateway_tool
    # can burn ~90s (45s x2 retries). Measuring the deadline from t0 (not from the
    # start of polling) bounds arm + poll together, so a slow arm can never combine
    # with a full poll to exceed the 300s worker ceiling and strand a 'running' row.
    t0 = time.monotonic()
    deadline = t0 + budget_seconds

    with record_run(SUITE, trigger) as run:
        tenant = resolve_journey_tenant()

        # No delivery precondition to set up any more. An explicitly configured
        # eval-sink tenant resolves to ``eval`` (gated on ``is_eval_sink``), so
        # CronDeliveryView writes the
        # ``ProactiveOutbound`` row this probe asserts on. The fabricated APNs
        # DeviceToken this used to plant before every arm — and which every
        # successful fire then destroyed, alternating pass/fail forever — is gone.

        # Open the observation window BEFORE arming: any ProactiveOutbound created
        # from here on is in-window; anything older is excluded. Combined with the
        # unique job_name, this is what stops a stale row reading green forever.
        window_start = timezone.now()
        job_name = _unique_job_name()

        try:
            _arm_one_shot_cron(tenant, name=job_name, lead_seconds=lead_seconds)
        except (TypedCronError, GatewayError):
            # Could not even register/push the cron (bad payload, unreachable
            # container). A real failure — record it so the run closes FAIL and the
            # owner is alerted, rather than crashing into a bare ERROR.
            logger.exception("journey_cron: failed to arm one-shot cron")
            record(
                run,
                CASE_ID,
                EvalResult.Kind.JOURNEY,
                passed=False,
                details={
                    "armed": False,
                    "delivered": False,
                },
            )
        else:
            obs = _observe_delivery(
                tenant,
                job_name=job_name,
                window_start=window_start,
                deadline=deadline,
                interval_seconds=interval_seconds,
                sleep_fn=sleep_fn,
            )
            record(
                run,
                CASE_ID,
                EvalResult.Kind.JOURNEY,
                passed=obs.delivered,
                details={
                    "armed": True,
                    "delivered": obs.delivered,
                    "poll_count": obs.poll_count,
                    "observe_ms": obs.elapsed_ms,
                    "lead_s": int(lead_seconds),
                    "budget_s": int(budget_seconds),
                },
            )

    return run
