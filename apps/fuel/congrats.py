"""Workout-completion congratulations — schedule an agent-authored "nice work" note.

Fail-soft trigger called from the JWT workout-completion views (console / iOS). When a
user genuinely completes a workout, we schedule a one-shot ``workout_congrats`` cron
that fires ~15s later; at fire time the tenant's assistant authors ONE short, warm,
personal congratulations and sends it through the existing proactive-message machinery
(``CronDeliveryView`` → LINE / iOS APNs push + ?since= chat feed). The message is always
agent-authored — never a hardcoded client string.

Policy (MJ-approved): congratulate EVERY genuine completion, guarded by four layers:

  1. ``Workout.congratulated_at`` (durable, per-workout) — stamped atomically on the
     first done-transition, so a done→planned→done re-toggle never re-fires.
  2. Per-tenant cooldown (~45 min) — reuses that same stamp as a tenant-wide clock:
     if ANY of this tenant's workouts was congratulated within the window, skip. This
     is the cheapest reliable cooldown check — no extra per-tenant state to maintain,
     and it reads the column we're already writing.
  3. Recency gate (~2 days) — a backfilled old session (HealthKit import, a manually
     backdated log) is a real completion but not a "just now" one; don't celebrate it.
  4. The existing 20/hour ``CronDeliveryView`` cap — the hard backstop (no change here).

Only the JWT views hook this. The runtime/agent paths and HealthKit ingest are
excluded structurally (they never call in) so agent self-completions and batch
backfills never congratulate.

Nothing here may break or slow the workout save: the public entry point is wrapped
fail-soft and the slow gateway push is dispatched OFF the request path.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.common.llm_contracts import today_in_tenant_tz

from .models import PersonalRecord, Workout

logger = logging.getLogger(__name__)

# Fire the congrats cron shortly after completion — long enough that the workout
# save + any follow-on writes have settled, short enough to feel immediate.
CONGRATS_FIRE_DELAY_SECONDS = 15

# Per-tenant cooldown: at most one congrats within this window. Someone logging a
# few sessions back-to-back gets one warm note, not a burst.
CONGRATS_COOLDOWN_MINUTES = 45

# Only celebrate sessions dated within this many days of the tenant's local today,
# so a backfilled old workout (imported / backdated) doesn't trigger a stale congrats.
CONGRATS_RECENCY_DAYS = 2

# Units for the optional PR summary line. Local (not imported from services) to keep
# this cosmetic best-effort path decoupled from the trends internals.
_PR_UNITS = {"est_1rm": " kg", "distance": " km", "hold_s": " s", "reps": " reps"}


def maybe_congratulate_workout(tenant, workout, transitioned_to_done: bool) -> None:
    """Fire-soft entry point: schedule a congrats note for a genuine completion.

    Never raises and never meaningfully slows the caller — all the checks are cheap
    indexed reads plus one conditional UPDATE, and the slow gateway push runs off the
    request path. ``transitioned_to_done`` is the caller's signal that THIS request is
    the one that moved the workout into ``done`` (see the view hooks); when False,
    there is nothing to celebrate.
    """
    try:
        _maybe_congratulate_workout(tenant, workout, transitioned_to_done)
    except Exception:
        logger.warning(
            "workout congrats hook failed (non-fatal) tenant=%s workout=%s",
            getattr(tenant, "id", "?"),
            getattr(workout, "id", "?"),
            exc_info=True,
        )


def _maybe_congratulate_workout(tenant, workout, transitioned_to_done: bool) -> None:
    if not transitioned_to_done:
        return
    if tenant is None or workout is None:
        return

    # Layer 1: durable per-workout guard. A re-toggle (done→planned→done) or a repeat
    # POST to /complete/ finds the stamp already set and stops here.
    if workout.congratulated_at is not None:
        return

    # Layer 3: recency gate. A future or very-recent date passes; a backfilled old
    # session (days in the past) does not.
    today = today_in_tenant_tz(tenant)
    if workout.date is None or (today - workout.date).days > CONGRATS_RECENCY_DAYS:
        return

    # Layer 2: per-tenant cooldown, read off the shared congratulated_at clock. Checked
    # BEFORE we stamp this workout, so this workout's own (still-null) stamp can't trip
    # it and only prior congrats count.
    cooldown_cutoff = timezone.now() - timedelta(minutes=CONGRATS_COOLDOWN_MINUTES)
    if Workout.objects.filter(tenant=tenant, congratulated_at__gte=cooldown_cutoff).exists():
        return

    # Atomic claim: compare-and-set the durable stamp. If a concurrent completion
    # already claimed it (0 rows updated), that request owns the congrats — bail.
    now = timezone.now()
    claimed = Workout.objects.filter(id=workout.id, congratulated_at__isnull=True).update(congratulated_at=now)
    if not claimed:
        return
    workout.congratulated_at = now  # keep the in-memory instance consistent

    # Build the PII-safe fact payload on the request path (cheap), then push the
    # cron off it (slow gateway call).
    payload = _build_payload(workout)
    _dispatch_schedule(tenant, workout_id=str(workout.id), payload=payload)


def _build_payload(workout) -> dict:
    """Assemble the minimal, PII-safe fact set embedded in the cron payload.

    Structured fields only — never the free-text notes, since cron prompts bypass
    inbound PII redaction.
    """
    payload: dict = {"activity": (workout.activity or "").strip()[:128]}
    if workout.category:
        payload["category"] = workout.category
    if workout.duration_minutes is not None:
        payload["duration_minutes"] = workout.duration_minutes
    if workout.rpe is not None:
        payload["rpe"] = workout.rpe
    pr_summary = _pr_summary_for(workout)
    if pr_summary:
        payload["pr_summary"] = pr_summary
    return payload


def _pr_summary_for(workout) -> str:
    """One-line summary of any PR set in this session (detect_prs ran before us)."""
    prs = list(PersonalRecord.objects.filter(workout=workout).order_by("-value")[:2])
    if not prs:
        return ""
    parts: list[str] = []
    for pr in prs:
        unit = _PR_UNITS.get(pr.metric, "")
        cur = _fmt_num(pr.value)
        if pr.previous_value is not None:
            parts.append(f"{pr.exercise_name} {cur}{unit} (prev {_fmt_num(pr.previous_value)}{unit})")
        else:
            parts.append(f"{pr.exercise_name} {cur}{unit}")
    return ("New PR: " + "; ".join(parts))[:200]


def _fmt_num(value) -> str:
    """Render a Decimal/number without noisy trailing zeros (100.00 → '100')."""
    return f"{float(value):g}"


def _dispatch_schedule(tenant, *, workout_id: str, payload: dict) -> None:
    """Schedule the congrats cron OFF the request path.

    Mirrors ``apps.router.proactive_context._dispatch_ios_push``: a background daemon
    thread in prod (the gateway push can take up to ~90s with retry), synchronous under
    ``NBHD_DISABLE_BACKGROUND_THREADS`` (tests) for determinism. Fail-soft.
    """

    def _run() -> None:
        try:
            _schedule_congrats_cron(tenant, workout_id=workout_id, payload=payload)
        except Exception:
            logger.warning(
                "workout congrats scheduling failed (non-fatal) tenant=%s workout=%s",
                getattr(tenant, "id", "?"),
                workout_id,
                exc_info=True,
            )

    if getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False):
        _run()
    else:
        threading.Thread(target=_run, daemon=True).start()


def _schedule_congrats_cron(tenant, *, workout_id: str, payload: dict) -> None:
    """Create the one-shot ``workout_congrats`` at-cron via the typed-cron service.

    Local imports: the cron service pulls in the patterns package (which touches Django
    models), so defer to avoid a fuel→cron import cycle at module load.
    """
    from apps.cron.models import CronJobSource, CronPattern
    from apps.cron.services import create_typed_cron

    fire_at = timezone.now() + timedelta(seconds=CONGRATS_FIRE_DELAY_SECONDS)
    # Hyphen, not colon: the workout uuid carries hyphens only, and a hyphenated name
    # is safe even if it ever feeds a QStash dedup id (which rejects ':' / whitespace).
    name = f"_congrats-{workout_id}"

    create_typed_cron(
        tenant=tenant,
        pattern=CronPattern.WORKOUT_CONGRATS,
        typed_payload=payload,
        name=name,
        schedule={"kind": "at", "at": fire_at.isoformat()},
        source=CronJobSource.SYSTEM,
    )
