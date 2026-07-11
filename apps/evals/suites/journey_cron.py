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
out and the run polls up to ``deadline_seconds``, both well under the 300s
gunicorn worker ceiling (``config/settings/base.py`` / ``startup.sh``). If prod
fire-verification ever shows a single request brushing that ceiling (e.g. cold
starts add latency), the documented fallback is two-phase — arm on run N, observe
run N+1 — but the primary design here is single-phase because it gives a true
same-run end-to-end assertion.

INVARIANT #1: nothing recorded here is user content. The reminder text is a fixed
synthetic string sent to the synthetic tenant's own user; the eval sink only ever
sees existence, counts and durations — never the message body.
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

# Timing budget (all well under the 300s worker ceiling — see module docstring).
# ``lead_seconds`` mirrors the plan's "60-90s out"; ``deadline_seconds`` caps the
# poll at ~240s leaving ~60s headroom for arming + close.
SCHEDULE_LEAD_SECONDS = 75
POLL_DEADLINE_SECONDS = 240
POLL_INTERVAL_SECONDS = 5

# Fixed synthetic reminder text (no PII). Delivered to the synthetic tenant's own
# user via ``nbhd_send_to_user``; harmless, and the eval sink never sees it.
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
    deadline_seconds: float,
    interval_seconds: float,
    sleep_fn=time.sleep,
) -> DeliveryObservation:
    """Poll for the ONE piece of evidence a cron fired: a fresh ProactiveOutbound.

    Returns metadata only (delivered flag + poll count + elapsed ms) — never any
    row content. The filter is the whole point of the probe: unique ``job_name``
    AND ``created_at__gte=window_start`` so neither a stale historical row nor an
    unrelated concurrent cron's row can satisfy it.
    """
    from apps.router.models import ProactiveOutbound

    start = time.monotonic()
    deadline = start + deadline_seconds
    poll_count = 0
    delivered = False
    while True:
        poll_count += 1
        delivered = ProactiveOutbound.objects.filter(
            tenant=tenant,
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
    deadline_seconds: float = POLL_DEADLINE_SECONDS,
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

    with record_run(SUITE, trigger) as run:
        tenant = resolve_journey_tenant()

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
                details={"armed": False, "delivered": False},
            )
        else:
            obs = _observe_delivery(
                tenant,
                job_name=job_name,
                window_start=window_start,
                deadline_seconds=deadline_seconds,
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
                    "deadline_s": int(deadline_seconds),
                },
            )

    return run
