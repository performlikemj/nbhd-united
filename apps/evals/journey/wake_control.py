"""Force-hibernate a single tenant and CONFIRM it via Azure ground truth (PR-B4).

Probe 4 (hibernation-wake, docs/evals-wave-b-plan.md) can only test the wake
path if the tenant is genuinely asleep BEFORE the probe sends its message. No
single-tenant force-hibernate entry point exists today: ``force_hibernate_stale``
is a bulk management sweep, and ``hibernate_idle_tenant`` is the idle-service
primitive. This module is the thin wrapper the plan calls for — it drives the
REAL ``hibernate_idle_tenant(tenant)`` path (capture crons → suspend → deactivate
revisions → mark ``hibernated_at``) and then verifies the container is actually
down.

Why "confirm via Azure, not just the flag" (docs/evals-wave-b-plan.md Probe 4):
``Tenant.hibernated_at`` drifts from Azure in BOTH directions — the flag can say
hibernated while a revision is still active, or say awake while Azure shows zero
active revisions (pending_queue.py:972-1000 documents the drift). So the
ground-truth signal this module trusts is Azure's revision list via
``container_app_has_active_revision`` — **zero active revisions** — not the DB
flag. The flag is read too, but only as a drift datapoint in the result.

INVARIANT #3 (docs/evals-directive.md): a probe that cannot establish its
precondition FAILS loudly — it never silently skips. So this returns a
``HibernateResult`` whose ``hibernated`` is True ONLY when Azure confirmed zero
active revisions within a bounded retry; the suite records a FAILING case (not a
skip) when it stays False. "Something keeps waking it" is a real finding.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from apps.orchestrator.azure_client import container_app_has_active_revision
from apps.orchestrator.hibernation import hibernate_idle_tenant

logger = logging.getLogger(__name__)

# Force-hibernate + ground-truth-confirm budget. The whole precondition runs in
# the SAME gunicorn worker as the ~240s wake drive that follows and must clear
# the 300s worker ceiling together (docs/evals-wave-b-plan.md fact #2), so this
# stays tight: at most ``_MAX_HIBERNATE_ATTEMPTS`` force-hibernate attempts, each
# confirmed by a short Azure poll (``_CONFIRM_POLLS`` reads, ``_CONFIRM_INTERVAL``
# apart) to absorb the lag between ``deactivate_revision`` and the revision list
# reflecting it.
_MAX_HIBERNATE_ATTEMPTS = 2
_CONFIRM_POLLS = 4
_CONFIRM_INTERVAL_SECONDS = 3.0

# Hard WALL-CLOCK cutoff on the whole precondition. The confirm-poll sleeps are
# NOT the dominant cost: ``hibernate_idle_tenant`` makes two per-tenant gateway
# calls (cron.list + suspend_tenant_crons), and against a WEDGED-but-connected
# container each can hang ~45s (gateway_client timeout) — a down container 404s
# fast, so this only bites the wedged case. Left unbounded, one wedged hibernate
# eats 90s+ and, stacked with the ~240s drive, SIGKILLs the worker at 300s →
# stranded 'running' run + QStash re-run of the whole cycle. So once elapsed
# exceeds this, we stop and return a clean Gate-1 FAIL (the container being that
# slow to hibernate is itself a real finding). 45s (not 60) keeps 45 + the 240s
# drive = 285s under the 300s ceiling with margin; we only ever start the drive
# when the precondition came in UNDER this.
_WALL_CLOCK_BUDGET_SECONDS = 45.0


@dataclass
class HibernateResult:
    """Outcome of a force-hibernate attempt. Metadata only — never content.

    ``hibernated`` is the load-bearing field: True IFF Azure confirmed zero
    active revisions (the ground truth the wake probe requires), NOT merely that
    the DB flag got stamped.
    """

    # Ground truth: Azure showed 0 active revisions within the bounded retry.
    hibernated: bool = False
    # How many force-hibernate attempts were made (1.._MAX_HIBERNATE_ATTEMPTS).
    attempts: int = 0
    # Drift datapoint: was ``Tenant.hibernated_at`` non-null after? (Can disagree
    # with ``hibernated`` in either direction — that disagreement is the finding.)
    flag_set: bool = False
    # Where it gave up, for triage: "" (confirmed) | "hibernate_call" (the real
    # path raised) | "azure_active" (a revision stayed active past the retry) |
    # "budget_exceeded" (the wall-clock cutoff tripped before confirmation).
    failure_stage: str = ""


def _azure_confirmed_hibernated(
    container_id: str,
    *,
    polls: int = _CONFIRM_POLLS,
    interval_seconds: float = _CONFIRM_INTERVAL_SECONDS,
    sleep=time.sleep,
) -> bool:
    """Poll Azure until it reports 0 active revisions, or the poll budget runs out.

    Returns True the moment ``container_app_has_active_revision`` reads False
    (ground-truth hibernated). The first read is immediate; a short sleep between
    reads absorbs the lag between ``deactivate_revision`` returning and the
    revision list reflecting it. ``sleep`` is injectable so tests don't wait.

    A raised read (a transient ARM/network blip) is treated as a FAILED poll —
    "still active", keep polling — NOT propagated: an uncaught raise here would
    close the whole run ``error`` (a crash + page + QStash retry) on a blip. If
    every poll raises, this returns False and the caller's retry / clean Gate-1
    FAIL handles it; a single blip is absorbed by the next read.
    """
    for i in range(polls):
        try:
            active = container_app_has_active_revision(container_id)
        except Exception:
            logger.warning(
                "journey_wake: Azure revision read raised (poll %d/%d) — treating as unconfirmed", i + 1, polls
            )
            active = True  # a failed read is NOT a confirmation of hibernation
        if not active:
            return True
        if i < polls - 1:
            sleep(interval_seconds)
    return False


def force_hibernate_and_confirm(
    tenant,
    *,
    max_attempts: int = _MAX_HIBERNATE_ATTEMPTS,
    confirm_polls: int = _CONFIRM_POLLS,
    confirm_interval_seconds: float = _CONFIRM_INTERVAL_SECONDS,
    budget_seconds: float = _WALL_CLOCK_BUDGET_SECONDS,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> HibernateResult:
    """Force ``tenant`` hibernated and confirm it via Azure ground truth.

    Drives the REAL ``hibernate_idle_tenant`` path (not a shortcut), then confirms
    the container is down by reading Azure's revision list — retrying the whole
    cycle up to ``max_attempts`` times if a revision stays active (e.g. a
    concurrent probe or a stale read keeps it awake). The DB flag is NOT trusted
    as the signal; it is only recorded as a drift datapoint.

    Bounded by a hard ``budget_seconds`` wall clock: ``hibernate_idle_tenant``'s
    gateway calls can hang on a wedged container, so once the elapsed time exceeds
    the budget we stop and return a clean Gate-1 FAIL rather than start the ~240s
    drive and SIGKILL the worker at the 300s ceiling. ``monotonic``/``sleep`` are
    injectable so tests neither wait nor depend on the real clock.

    Returns a ``HibernateResult`` with ``hibernated=True`` ONLY on Azure
    confirmation within budget. A False result is a real precondition FAILURE for
    the caller to record (INVARIANT #3) — never a silent skip.
    """
    tid = str(getattr(tenant, "id", ""))[:8]
    result = HibernateResult()
    start = monotonic()

    for attempt in range(1, max_attempts + 1):
        # Don't start another attempt if a prior one already spent the budget —
        # there would be no wall-clock left for the drive.
        if monotonic() - start > budget_seconds:
            result.failure_stage = "budget_exceeded"
            logger.warning("journey_wake: tenant %s hibernate budget exhausted before attempt %d", tid, attempt)
            break
        result.attempts = attempt
        try:
            # The real single-tenant hibernation path: capture crons, suspend
            # them, deactivate all revisions, stamp hibernated_at. Its bool return
            # is NOT trusted here — Azure is the ground truth (the flag drifts).
            hibernate_idle_tenant(tenant)
        except Exception:
            logger.exception("journey_wake: hibernate_idle_tenant raised for tenant %s (attempt %d)", tid, attempt)
            result.failure_stage = "hibernate_call"
            continue

        # A wedged container's gateway calls inside hibernate_idle_tenant can burn
        # ~45s×2; if that consumed the budget, fail cleanly NOW rather than begin
        # the ~240s drive and blow the 300s ceiling (leaves the container safely
        # hibernated — the next run finds it down and confirms fast).
        if monotonic() - start > budget_seconds:
            result.failure_stage = "budget_exceeded"
            logger.warning("journey_wake: tenant %s hibernate exceeded budget after attempt %d", tid, attempt)
            break

        if _azure_confirmed_hibernated(
            tenant.container_id,
            polls=confirm_polls,
            interval_seconds=confirm_interval_seconds,
            sleep=sleep,
        ):
            result.hibernated = True
            result.failure_stage = ""
            break

        # A revision is still active after hibernating — something is keeping the
        # container awake. Retry the whole cycle; if it never confirms, the caller
        # records a FAIL (the plan: "if it can't hibernate after bounded retry
        # ... that's a real FAIL, not a skip").
        result.failure_stage = "azure_active"
        logger.warning(
            "journey_wake: tenant %s still shows an active revision after hibernate (attempt %d/%d)",
            tid,
            attempt,
            max_attempts,
        )

    # Record the DB flag purely as a drift datapoint (it can disagree with the
    # Azure ground truth in either direction).
    try:
        tenant.refresh_from_db(fields=["hibernated_at"])
        result.flag_set = getattr(tenant, "hibernated_at", None) is not None
    except Exception:
        logger.exception("journey_wake: could not re-read hibernated_at for tenant %s", tid)
        result.flag_set = False

    return result
