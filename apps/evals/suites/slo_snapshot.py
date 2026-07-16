"""Suite 4 — production SLO snapshot (metadata only) + weekly digest.

docs/evals-directive.md §Suite 4. A nightly, DB-metadata-only readout of how the
live product is actually behaving, recorded through the same chassis every other
suite uses: one ``EvalRun`` (suite ``slo_snapshot``), one ``EvalResult`` per
metric (``score`` = the measured value, ``threshold`` from config, ``passed`` =
within threshold). A metric that BREACHES its threshold makes the run close
``fail`` → ``finalize_task_run`` alerts the owner + DLQs the delivery. That is the
"breach flagged" mechanism; there is no separate alarm path.

Two properties this suite must never violate:

  * **Metadata only (INVARIANT #1).** Every query below selects timestamps,
    statuses, and counts — ``created_at`` / ``replied_at`` / ``waking_at`` /
    ``status`` — and NEVER ``user_text`` / ``reply_text`` / any ``*_enc`` column.
    Reading a message body is a defect, not a variant: an SLO snapshot has no
    business decrypting anything. The details sidecar rides the same
    ``_assert_details_safe`` chokepoint as every other suite (counts/ids/labels,
    short strings only), so a transcript cannot leak even by accident.

  * **Synthetic traffic must not pollute production SLOs.** ``Tenant.is_synthetic``
    is a standing invariant (evals-directive §1.5): the journey/behavior probes
    drive real containers, so their turns land in ``AppChatMessage`` /
    ``ProactiveOutbound`` exactly like a subscriber's — and a synthetic tenant's
    deliberately-slow wake probe would drag production p95 if we counted it. Every
    metric here filters ``tenant__is_synthetic=False``.

Missing data is NOT healthy. An empty window for a latency/rate metric (zero
qualifying turns in 24h) is recorded **skipped-with-reason** — ``score=None`` +
``details.skipped=True`` — never a passing ``score=0`` that reads as "perfect
latency". An empty table may mean a broken writer, so a skip is a visible
finding, not a silent green (it just does not, on its own, breach the run).

Deferred by design (named here so a reviewer sees they were considered, not
missed) — each needs infra Django cannot reach from a task:
  * **decrypt-audit events by principal** — ``apps/crypto/audit.py`` is a *stdout
    JSON logger* (``nbhd.decrypt_audit``), deliberately NOT a DB table
    (red-team 6/17: a DB audit trail is rewritable by anyone with DB creds). It
    lives in Log Analytics ``ContainerAppConsoleLogs_CL`` only. An owner-read
    spike is a real finding, but it is a Log-Analytics query, not a DB one.
  * **QStash DLQ depth** — ``apps/cron/publish.py`` has only a *publish* client;
    there is no server-side DLQ-read helper, and reaching Upstash's DLQ API from
    inside the snapshot would add a new external call this task should not make.
    The DB-derivable proxy for "a probe DLQ'd" is ``eval_run_error_count`` below.
  * **the 20s-timeout-burst pattern** — a Log-Analytics signal over container
    request logs, not a DB fact.
  * **CryptoError count** — a ``box.py`` decrypt/tamper exception surfaces as a
    Sentry event / stdout log line, not a counted DB row; it belongs to the same
    Log-Analytics follow-up.
  * **full cold-path user-perceived latency** — ``created_at → replied_at`` for turns
    that woke a container. Since 2026-07-14 ``compute_reply_latency`` excludes woken
    turns (they were being judged twice, and losing to the warm ceiling), and
    ``compute_wake_latency_p95`` measures only the WAKE PORTION
    (``waking_at → replied_at``). So the number a user actually experiences on a cold
    start — the full wait, wake plus turn — is deliberately not a metric here. It is
    NOT unmeasured: the journey-wake canary (Probe 4) drives that path end-to-end
    against its own SLO, and ``n_woken`` in the reply-latency details says how many
    real turns took it. Naming it so a future reader sees the seam was chosen, not
    missed.
  * **cron success RATE** — ``ProactiveOutbound`` only records deliveries that
    HAPPENED (plus excluded eval evidence), and it records every proactive producer
    (cron fires, meditation-ready, nightly_extraction), so the DB holds neither a
    cron-only numerator nor an
    attempted-fires denominator. Filtering by ``job_name`` doesn't help — the
    non-cron writers set one too. A true success rate needs the fire-attempt log
    (Log Analytics), so it rides the same follow-up batch. Until then the
    journey-cron canary (Probe 3) is the real cron-health signal; the
    ``proactive_delivery_count_24h`` metric below is honest about being a raw
    proactive-delivery volume, not cron health.
These land in the admin-console trends page follow-up, not here.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.evals.models import EvalResult, EvalRun
from apps.evals.runner import record, record_run
from apps.router.models import AppChatMessage, ProactiveOutbound

logger = logging.getLogger(__name__)

SUITE = "slo_snapshot"

# The rolling window every metric measures over.
WINDOW_HOURS = 24
# The trailing span the weekly digest trends over.
DIGEST_WINDOW_DAYS = 7

# Minimum sample sizes below which a percentile is NOT a percentile.
#
# Prod, 2026-07-13: the whole 24h window held **16 real turns across the entire
# fleet** — 14 of them one user's. At n=16 the type-7 "p95" is an interpolation
# between the 15th and 16th sorted values: roughly the second-slowest turn of the day
# wearing a statistical costume. One slow turn moves it enormously, so the metric
# breaches on any day somebody waits — and a metric that always breaches gets ignored,
# which is strictly worse than not having it.
#
# Why the p95 floor is 40 and not 20. n>=20 is the EXISTENCE bar (1/0.05), not the
# honesty bar: at exactly n=20 the type-7 rank is 0.95*(20-1) = 18.05, so the estimate
# STILL interpolates between the second-largest and the largest observation — the same
# near-max-in-a-costume described above, merely legal. The top 5% does not contain two
# distinct observations until n=40, which is where the estimator stops being dominated
# by a single slow turn. A single-slow-turn alarm is the ignored-alarm disease this
# floor exists to cure, so the floor has to be past it.
#
# It costs nothing today: at ~16 turns/day BOTH floors skip forever. The number only
# decides the DATE the metric un-skips as the fleet grows (~13 MJ-usage subscribers at
# 40, ~7 at 20) — and at 40, the day it un-skips, it will mean something.
#
# A median is far more forgiving and is not dragged down with the tail.
#
# Below the floor the metric is recorded SKIPPED-WITH-REASON (score=None,
# details.skipped=True, details.n=<actual>, details.floor=<floor>) — the suite's
# standing rule that missing data is SURFACED, never passed off as a green zero,
# applies just as much to *insufficient* data as to none. A persistently-skipped p95 is
# itself the finding: the fleet does not yet produce enough turns in 24h to have a
# measurable tail. That is a true statement about the business, and it belongs in the
# open rather than papered over with a number that means nothing.
#
# NAMED FOLLOW-UP (not this PR): a 7-DAY rolling p95 (~112 samples today) computed
# nightly is the structurally right way to actually HAVE a tail — daily p50 as the fast
# robust alarm, 7-day p95 as the slow tail alarm. It must get its OWN case_id
# (``reply_latency_p95_7d_ms``), never this one: a trend query must never blend 24h and
# 7d window semantics under a single name. This daily p95 then stays as the tombstone,
# self-activating if the fleet ever earns a daily tail.
MIN_SAMPLE_P50 = 10
MIN_SAMPLE_P95 = 40

# The flagship journey canaries that treat a synthetic-tenant personal
# budget-cap trip as a SOFT pass (journey_chat.py / journey_wake.py). That design
# has a failure mode: once the synthetic tenant's ~$10 monthly cap trips, these
# canaries STOP exercising the real pipeline for the rest of the month while still
# reading green-ish — so a fully-capped canary is effectively offline. The
# journey-budget-capped metric below is the tripwire for exactly that (journal /
# cron probes have no budget soft-pass semantics, so they are not listed here).
JOURNEY_PROBE_SUITES = ("journey_chat", "journey_wake")
# The details marker both suites stamp on their soft budget-capped result.
_BUDGET_EXHAUSTED_MARKER = "budget_exhausted"

# Metric (case) ids — stable, greppable, and the digest's row keys.
M_REPLY_P50 = "reply_latency_p50_ms"
M_REPLY_P95 = "reply_latency_p95_ms"
M_WAKE_P95 = "wake_latency_p95_ms"
M_ERROR_RATE = "error_message_rate"
# Named for what the table actually holds: EVERY record_proactive_outbound
# producer writes ProactiveOutbound rows — real cron fires
# (apps/router/cron_delivery.py), the meditation-ready push
# (apps/core/services.py), AND nightly_extraction (apps/journal/extraction.py,
# ~one row per active tenant per night) — so this is proactive-delivery VOLUME,
# not a cron-health signal: a dead cron pipeline would hide behind extraction
# rows forever. The true cron success rate is a NAMED deferral (module
# docstring); do not "fix" this by filtering job_name — non-cron writers set it.
M_PROACTIVE_DELIVERIES = "proactive_delivery_count_24h"
M_EVAL_RUN_ERRORS = "eval_run_error_count"
M_JOURNEY_BUDGET_CAPPED = "journey_budget_capped_ratio"

# Every metric the snapshot RECORDS, in digest display order.
METRIC_IDS = (
    M_REPLY_P50,
    M_REPLY_P95,
    M_WAKE_P95,
    M_ERROR_RATE,
    M_PROACTIVE_DELIVERIES,
    M_EVAL_RUN_ERRORS,
    M_JOURNEY_BUDGET_CAPPED,
)

# Sane in-code defaults; ``settings.EVAL_SLO_THRESHOLDS`` (env JSON) overrides any
# subset. Latencies in milliseconds. Breach direction is per-metric (see below).
DEFAULT_SLO_THRESHOLDS: dict[str, float] = {
    # p50 tight, p95 at the 45s round-trip SLO (chat_drive.DEFAULT_SLO_SECONDS).
    M_REPLY_P50: 15000,
    M_REPLY_P95: 45000,
    # The wake path is historically the slowest — a cold container spin-up rides
    # on top of the turn — so its ceiling is deliberately higher.
    M_WAKE_P95: 90000,
    # At most 5% of finished real turns may terminate ``error``.
    M_ERROR_RATE: 0.05,
    # A FLOOR (deliveries must be >= this). Default 0 = informational: it trends
    # in the digest without alarming on a legitimately quiet day. NOTE: because
    # nightly_extraction writes a row per active tenant, raising this floor
    # catches "every proactive writer is dead", not a dead cron pipeline
    # specifically — see the cron-success-rate deferral in the module docstring.
    M_PROACTIVE_DELIVERIES: 0,
    # Any stranded/error EvalRun in the window is a finding.
    M_EVAL_RUN_ERRORS: 0,
    # Fraction of a flagship canary's runs that were budget-capped soft passes.
    # 0.5 = a MAJORITY: once more than half of a probe's runs are just hitting the
    # synthetic cap, that canary is effectively offline (a fully-tripped cap → 1.0).
    M_JOURNEY_BUDGET_CAPPED: 0.5,
}

# Metrics whose threshold is a CEILING: breach when measured value > threshold.
_CEILING_METRICS = frozenset(
    {M_REPLY_P50, M_REPLY_P95, M_WAKE_P95, M_ERROR_RATE, M_EVAL_RUN_ERRORS, M_JOURNEY_BUDGET_CAPPED}
)
# Metrics whose threshold is a FLOOR: breach when measured value < threshold.
_FLOOR_METRICS = frozenset({M_PROACTIVE_DELIVERIES})


def thresholds() -> dict[str, float]:
    """Effective thresholds: code defaults overlaid by ``settings.EVAL_SLO_THRESHOLDS``.

    Only keys already in the defaults are honored from settings, so a typo'd env
    key can never introduce a phantom metric — it is dropped (with a warning log
    line naming the key, so the misconfiguration is visible), not recorded.
    """
    override = getattr(settings, "EVAL_SLO_THRESHOLDS", None) or {}
    merged = dict(DEFAULT_SLO_THRESHOLDS)
    for key, value in override.items():
        if key in DEFAULT_SLO_THRESHOLDS:
            merged[key] = value
        else:
            # A typo'd override silently reverting to the default is exactly the
            # kind of quiet misconfiguration this suite exists to surface.
            logger.warning("slo thresholds: unknown key %r in EVAL_SLO_THRESHOLDS ignored", key)
    return merged


def percentile(values: list[float], p: float) -> float | None:
    """Type-7 (linear-interpolation) percentile of ``values`` at ``p`` in [0, 100].

    Returns ``None`` for an empty input — the caller records that as
    skipped-with-reason, never as ``0`` (a fake zero would read as a perfect
    latency; see the module docstring).
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return float(ordered[0])
    rank = (p / 100.0) * (n - 1)
    lo = int(rank)  # floor
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return float(ordered[lo]) + (float(ordered[hi]) - float(ordered[lo])) * frac


def _window(now):
    """The ``[now - WINDOW_HOURS, now]`` bounds, inclusive of the upper edge."""
    return now - timedelta(hours=WINDOW_HOURS), now


def compute_reply_latency(now) -> dict | None:
    """(p50, p95, n) of REAL-tenant, tenant-produced, WARM ready-turn latency (ms).

    METADATA ONLY: selects ``created_at`` / ``replied_at`` — never a body column.
    Filters to ``status=ready`` + ``source=tenant`` so a fabricated on-device
    reply (instant, source=on_device) and a still-pending turn cannot flatter the
    number, and excludes synthetic tenants. Returns ``None`` on an empty window.

    WOKEN TURNS ARE EXCLUDED (``waking_at__isnull=True``). A turn that had to spin
    up a hibernated container carries the cold start ON TOP of the turn, and
    ``compute_wake_latency_p95`` already measures exactly those turns against a
    deliberately higher ceiling (90s vs 45s) *because* the wake path is the slow one.
    Counting them here too meant the SAME turn was judged twice by two standards that
    disagree — and the reply SLO lost. Prod, 2026-07-13: a 95.4s turn PASSED the wake
    SLO (80.5s of it was the wake, under the 90s ceiling) while simultaneously
    breaching the 45s reply ceiling, and the p50 breach (16,198ms vs a 15,000ms
    threshold) was entirely an artifact of the two cold starts in that window —
    warm-only it was 14,812ms, i.e. green.

    What survives the exclusion is the honest signal, and it is not comfortable: two
    turns took 70.4s and 61.9s on ALREADY-WARM containers that day. Those are real.
    """
    since, until = _window(now)
    # ONE scan, split in Python. Two queries (warm rows, then a COUNT of woken ones)
    # would read the table at two instants, so a turn landing between them would make
    # ``n`` and ``n_woken`` describe different windows. Cosmetic — it could never affect
    # gating — but the single scan closes it for free.
    rows = AppChatMessage.objects.filter(
        tenant__is_synthetic=False,
        status=AppChatMessage.Status.READY,
        source=AppChatMessage.Source.TENANT,
        replied_at__isnull=False,
        created_at__gte=since,
        created_at__lte=until,
    ).values_list("created_at", "replied_at", "waking_at")

    latencies: list[float] = []
    n_woken = 0
    for created, replied, waking in rows.iterator():
        if created is None or replied is None or replied < created:
            continue
        if waking is not None:
            # EXCLUDED, not dropped. Counted so a reader can never mistake a thin warm
            # sample for a quiet fleet: "n=14, n_woken=2" says plainly that two real
            # users waited on a cold start, and that ``wake_latency_p95`` is where they
            # are accounted for.
            n_woken += 1
            continue
        latencies.append((replied - created).total_seconds() * 1000.0)

    if not latencies:
        # No WARM turns — but that covers two situations which read as OPPOSITES, and
        # collapsing them into one "no data" skip would report the reverse of the truth:
        #
        #   n_woken == 0  → the fleet genuinely was quiet. An absence.
        #   n_woken  > 0  → the fleet was NOT quiet. EVERY turn was a cold start. That
        #                   is a finding, and a loud one — reporting it as "no turns"
        #                   would tell the reader nothing happened when in fact every
        #                   user who showed up waited on a container spin-up.
        #
        # Entirely plausible at this fleet size: three turns, three wakes. The caller
        # records the second case with its own reason and carries n_woken, so the day
        # is legible from a single row instead of by cross-reading the wake metric.
        if n_woken == 0:
            return None
        return {"p50": None, "p95": None, "n": 0, "n_woken": n_woken}

    return {
        "p50": percentile(latencies, 50),
        "p95": percentile(latencies, 95),
        "n": len(latencies),
        "n_woken": n_woken,
    }


def compute_wake_latency_p95(now) -> dict | None:
    """(p95, n) of wake-path latency (``waking_at`` → ``replied_at``, ms), 24h.

    Only turns that actually woke a hibernated container have ``waking_at`` set,
    so this is the derivable wake SLO. METADATA ONLY; synthetic excluded; empty
    window → ``None`` (skipped, not a zero — no wakes is common on a warm fleet).

    THE p95 LABEL HERE IS ASPIRATIONAL, and deliberately so. Prod runs this at n=1-3 a
    day (n=2 on 2026-07-13), where the type-7 estimate interpolates between the only two
    observations — the very "percentile in a costume" that ``MIN_SAMPLE_P95`` exists to
    refuse for the reply metric. It gets NO floor anyway, and that is a choice, not an
    oversight:

      * wakes are structurally rare and you cannot grow wake volume without hurting
        users, so a floor of 40 would skip this metric permanently and delete the
        suite's ONLY real-user cold-start signal;
      * at tiny n it degenerates to "max wake vs the 90s ceiling" — which is exactly the
        question worth asking ("did any real user wait too long on a cold start?"). A
        one-slow-wake alarm is an alarm you WANT, unlike percentile noise.

    So: read this as a max-wake alarm, not a percentile, until the fleet is far larger.
    """
    since, until = _window(now)
    rows = AppChatMessage.objects.filter(
        tenant__is_synthetic=False,
        status=AppChatMessage.Status.READY,
        source=AppChatMessage.Source.TENANT,
        waking_at__isnull=False,
        replied_at__isnull=False,
        created_at__gte=since,
        created_at__lte=until,
    ).values_list("waking_at", "replied_at")

    latencies = [
        (replied - waking).total_seconds() * 1000.0
        for waking, replied in rows.iterator()
        if waking is not None and replied is not None and replied >= waking
    ]
    if not latencies:
        return None
    return {"p95": percentile(latencies, 95), "n": len(latencies)}


def compute_error_rate(now) -> dict | None:
    """(rate, errors, total) over FINISHED real turns in the window.

    Denominator is terminal turns (``ready`` or ``error``) — a still-``pending``
    recent turn is not a failure yet, so it is excluded rather than diluting the
    rate. METADATA ONLY (statuses + counts). Empty window (no finished turns) →
    ``None``: a rate is undefined with no denominator, and zero traffic may itself
    mean a broken writer, so it is surfaced as a skip, not a passing 0.0.

    DELIBERATELY UNFLOORED, unlike the reply percentiles. At this fleet's volume the
    ceiling is effectively an any-error alarm: 1 error in 16 finished turns is 6.25%,
    over the 5% threshold, so a single failure breaches and emails. That is kept on
    purpose — errors are rare and DISCRETE, and at this size every one of them is a
    genuine finding, so a one-error alarm is an alarm you want. (Percentile noise is
    the opposite: it manufactures alarms from ordinary variance, which is why the reply
    metrics get sample floors and this does not.) The 5% ceiling starts to mean what it
    says as volume grows.
    """
    since, until = _window(now)
    terminal = AppChatMessage.objects.filter(
        tenant__is_synthetic=False,
        source=AppChatMessage.Source.TENANT,
        status__in=[AppChatMessage.Status.READY, AppChatMessage.Status.ERROR],
        created_at__gte=since,
        created_at__lte=until,
    )
    total = terminal.count()
    if total == 0:
        return None
    errors = terminal.filter(status=AppChatMessage.Status.ERROR).count()
    return {"rate": errors / total, "errors": errors, "total": total}


def compute_proactive_deliveries(now) -> int:
    """Count of real-tenant ``ProactiveOutbound`` rows written in the window.

    This is TOTAL proactive-delivery volume, NOT cron health: every
    ``record_proactive_outbound`` producer writes here — real cron fires
    (apps/router/cron_delivery.py), the meditation-ready push
    (apps/core/services.py), and nightly_extraction (apps/journal/extraction.py,
    ~one row per active tenant per night) — so a dead cron pipeline stays hidden
    behind extraction rows. The honest cron-health signals are the journey-cron
    canary (Probe 3) and the deferred Log-Analytics cron success rate (module
    docstring). Always a real number (0 is a genuine measured count,
    floor-checked — not an empty-window skip). Synthetic excluded so the
    journey-cron probe's own deliveries never inflate it.
    """
    since, until = _window(now)
    return (
        ProactiveOutbound.objects.filter(
            tenant__is_synthetic=False,
            created_at__gte=since,
            created_at__lte=until,
        )
        .exclude(channel=ProactiveOutbound.Channel.EVAL)
        .count()
    )


def compute_eval_run_errors(now) -> dict:
    """(count, errors, stranded) of unhealthy EvalRuns visible in the window.

    ``errors`` = runs closed ``error`` (a suite that crashed or asserted nothing)
    started in the window. ``stranded`` = runs still ``running`` past the reaper's
    ``STUCK_RUN_TIMEOUT_MINUTES`` floor — a worker SIGKILL'd so hard that
    ``record_run``'s finally never ran; the daily reaper flips these, but a
    snapshot can legitimately catch one not-yet-reaped. A run that closed ``fail``
    is NOT counted — a fail is a probe correctly catching a real break (the system
    working), not an infra fault. Always a real number (0 = the healthy state).
    """
    from apps.evals.tasks import STUCK_RUN_TIMEOUT_MINUTES

    since, until = _window(now)
    stuck_cutoff = now - timedelta(minutes=STUCK_RUN_TIMEOUT_MINUTES)
    errors = EvalRun.objects.filter(
        status=EvalRun.Status.ERROR,
        started_at__gte=since,
        started_at__lte=until,
    ).count()
    # Stranded runs are not time-boxed to the 24h window: one stranded three days
    # ago is still a live problem until reaped. ``started_at`` is auto_now_add
    # (never NULL), so this ORDER-free filter dodges the NULLS-FIRST-on-DESC trap.
    stranded = EvalRun.objects.filter(
        status=EvalRun.Status.RUNNING,
        started_at__lte=stuck_cutoff,
    ).count()
    return {"count": errors + stranded, "errors": errors, "stranded": stranded}


def compute_journey_budget_capped(now) -> dict | None:
    """Worst per-probe fraction of flagship-canary runs that were budget-capped, 24h.

    Guards the "soft pass hides a dead canary" failure mode (B6 adversarial review):
    journey_chat/journey_wake record a budget-cap trip as a SOFT pass (passed=True,
    ``details.outcome == 'budget_exhausted'``), so once the synthetic tenant's ~$10
    monthly cap trips, those canaries stop exercising the real pipeline while still
    reading green. This measures, per probe, ``capped_runs / total_runs`` over the
    window and returns the WORST fraction across the flagship probes — so a single
    fully-capped canary (fraction 1.0) breaches even if the other is healthy.

    METADATA ONLY: reads ``EvalRun.suite`` + ``EvalResult.details.outcome`` (a
    machine code) + counts — never content. Returns ``None`` when NO flagship probe
    ran in the window (nothing to divide — the probes are inert until scheduled),
    recorded as skipped-with-reason rather than a misleading 0.0.
    """
    since, until = _window(now)
    per_probe: dict[str, dict[str, int]] = {}
    worst_ratio = 0.0
    worst_probe = ""
    any_runs = False

    for suite in JOURNEY_PROBE_SUITES:
        run_ids = list(
            EvalRun.objects.filter(
                suite=suite,
                started_at__gte=since,
                started_at__lte=until,
            ).values_list("id", flat=True)
        )
        total = len(run_ids)
        if total == 0:
            per_probe[suite] = {"soft": 0, "total": 0}
            continue
        any_runs = True
        # Distinct runs carrying a budget-capped soft-pass result. ``distinct()`` on
        # run_id so a probe that somehow recorded the marker twice counts once.
        soft = (
            EvalResult.objects.filter(
                run_id__in=run_ids,
                kind=EvalResult.Kind.JOURNEY,
                details__outcome=_BUDGET_EXHAUSTED_MARKER,
            )
            .values("run_id")
            .distinct()
            .count()
        )
        per_probe[suite] = {"soft": soft, "total": total}
        ratio = soft / total
        if ratio > worst_ratio:
            worst_ratio = ratio
            worst_probe = suite

    if not any_runs:
        return None

    chat = per_probe.get("journey_chat", {"soft": 0, "total": 0})
    wake = per_probe.get("journey_wake", {"soft": 0, "total": 0})
    return {
        "ratio": worst_ratio,
        "worst_probe": worst_probe,
        "chat_soft": chat["soft"],
        "chat_total": chat["total"],
        "wake_soft": wake["soft"],
        "wake_total": wake["total"],
    }


def _breaches(case_id: str, value: float, threshold: float) -> bool:
    """Direction-aware breach test: ceiling metrics breach high, floors breach low."""
    if case_id in _FLOOR_METRICS:
        return value < threshold
    return value > threshold


def _record_measured(run, case_id, value, threshold, details) -> None:
    record(
        run,
        case_id,
        EvalResult.Kind.SLO,
        passed=not _breaches(case_id, value, threshold),
        score=value,
        threshold=threshold,
        details={**details, "skipped": False},
    )


def _record_skipped(run, case_id, threshold, reason, **extra) -> None:
    """Record a metric we could not honestly measure as skipped-with-reason.

    ``passed=True`` (missing data is not, by itself, a threshold breach) but
    ``score=None`` and ``details.skipped=True`` make it unmistakably distinct from
    a real measured value — so the digest and any reader can tell "we couldn't
    measure this" from "this was 0 and fine".

    Covers BOTH no data and NOT ENOUGH data. ``extra`` carries the diagnostic that
    makes the skip actionable (e.g. ``n=16`` against a floor of 20) — counts only,
    so it rides the ``record()`` details chokepoint unchanged.
    """
    record(
        run,
        case_id,
        EvalResult.Kind.SLO,
        passed=True,
        score=None,
        threshold=threshold,
        details={"skipped": True, "reason": reason[:60], "window_h": WINDOW_HOURS, **extra},
    )


def run_slo_snapshot_suite(*, trigger: str = EvalRun.Trigger.MANUAL, now=None) -> EvalRun:
    """Compute every SLO metric over the last 24h and record it through the chassis.

    Returns the CLOSED run. ``image_tag=None`` — this suite touches no container.
    A metric breach → ``passed=False`` → the run closes ``fail`` (close_run), and
    the task wrapper's ``finalize_task_run`` turns that into an owner alert + DLQ.
    A crash mid-compute closes the run ``error`` via ``record_run`` and re-raises,
    so a snapshot that could not run FAILS loudly (INVARIANT #3), never green.
    """
    now = now or timezone.now()
    thr = thresholds()

    with record_run(SUITE, trigger, image_tag=None) as run:
        # --- WARM reply latency p50 / p95 (one query, two metrics, two sample floors) ---
        # Each percentile is gated on its own minimum sample: a median survives a thin
        # window, a 95th percentile does not. Below the floor we say so (skipped, with
        # the actual n) rather than publish a number that is really "the second-slowest
        # turn of the day" and let it breach a threshold it was never able to measure.
        latency = compute_reply_latency(now)
        if latency is None:
            # Genuinely no traffic at all.
            _record_skipped(run, M_REPLY_P50, thr[M_REPLY_P50], "no_ready_turns_24h")
            _record_skipped(run, M_REPLY_P95, thr[M_REPLY_P95], "no_ready_turns_24h")
        elif latency["n"] == 0:
            # Traffic existed and EVERY turn of it was a cold start. The opposite of a
            # quiet day, and it must not be reported as one.
            for cid in (M_REPLY_P50, M_REPLY_P95):
                _record_skipped(run, cid, thr[cid], "all_turns_were_cold_starts", n=0, n_woken=latency["n_woken"])
        else:
            n, woken = latency["n"], latency["n_woken"]
            if n < MIN_SAMPLE_P50:
                _record_skipped(
                    run, M_REPLY_P50, thr[M_REPLY_P50], "insufficient_sample", n=n, floor=MIN_SAMPLE_P50, n_woken=woken
                )
            else:
                _record_measured(
                    run, M_REPLY_P50, round(latency["p50"], 3), thr[M_REPLY_P50], {"n": n, "pctl": 50, "n_woken": woken}
                )

            if n < MIN_SAMPLE_P95:
                _record_skipped(
                    run, M_REPLY_P95, thr[M_REPLY_P95], "insufficient_sample", n=n, floor=MIN_SAMPLE_P95, n_woken=woken
                )
            else:
                _record_measured(
                    run, M_REPLY_P95, round(latency["p95"], 3), thr[M_REPLY_P95], {"n": n, "pctl": 95, "n_woken": woken}
                )

        # --- wake-path latency p95 ---
        wake = compute_wake_latency_p95(now)
        if wake is None:
            _record_skipped(run, M_WAKE_P95, thr[M_WAKE_P95], "no_wake_turns_24h")
        else:
            _record_measured(run, M_WAKE_P95, round(wake["p95"], 3), thr[M_WAKE_P95], {"n": wake["n"], "pctl": 95})

        # --- error-status message rate ---
        err = compute_error_rate(now)
        if err is None:
            _record_skipped(run, M_ERROR_RATE, thr[M_ERROR_RATE], "no_finished_turns_24h")
        else:
            _record_measured(
                run,
                M_ERROR_RATE,
                round(err["rate"], 3),
                thr[M_ERROR_RATE],
                {"errors": err["errors"], "total": err["total"]},
            )

        # --- proactive delivery volume (always a real count; floor-checked) ---
        deliveries = compute_proactive_deliveries(now)
        _record_measured(run, M_PROACTIVE_DELIVERIES, deliveries, thr[M_PROACTIVE_DELIVERIES], {"count": deliveries})

        # --- stranded/error EvalRun count (always a real count; 0 is healthy) ---
        run_errors = compute_eval_run_errors(now)
        _record_measured(
            run,
            M_EVAL_RUN_ERRORS,
            run_errors["count"],
            thr[M_EVAL_RUN_ERRORS],
            {"errors": run_errors["errors"], "stranded": run_errors["stranded"]},
        )

        # --- journey-canary budget-cap saturation (a capped canary is offline) ---
        capped = compute_journey_budget_capped(now)
        if capped is None:
            _record_skipped(run, M_JOURNEY_BUDGET_CAPPED, thr[M_JOURNEY_BUDGET_CAPPED], "no_journey_runs_24h")
        else:
            _record_measured(
                run,
                M_JOURNEY_BUDGET_CAPPED,
                round(capped["ratio"], 3),
                thr[M_JOURNEY_BUDGET_CAPPED],
                {
                    "worst_probe": capped["worst_probe"],
                    "chat_soft": capped["chat_soft"],
                    "chat_total": capped["chat_total"],
                    "wake_soft": capped["wake_soft"],
                    "wake_total": capped["wake_total"],
                },
            )

    return run


# --------------------------------------------------------------------------- #
# Weekly digest (Monday readout — sends even when all-green).
# --------------------------------------------------------------------------- #


def _metric_series(now):
    """Per-metric weekly series from the trailing ``DIGEST_WINDOW_DAYS`` snapshots.

    Returns ``(snapshot_count, run_status_counts, {case_id: {...}})``. For each
    metric: the latest value (or ``None`` if the latest snapshot skipped it),
    min/max over the week's measured (non-skipped) values, ``measured_days`` =
    how many snapshots actually MEASURED it (vs skipping on an empty window — an
    all-week skip must read 0/7, not hide behind one 'skip' in the latest
    column), and ``breach_days`` = how many snapshots recorded it
    ``passed=False``. Runs are ordered by ``started_at`` ASC (auto_now_add,
    never NULL — no NULLS-FIRST-on-DESC hazard).
    """
    since = now - timedelta(days=DIGEST_WINDOW_DAYS)
    runs = list(
        EvalRun.objects.filter(
            suite=SUITE,
            started_at__gte=since,
            started_at__lte=now,
        ).order_by("started_at")
    )
    status_counts = {"pass": 0, "fail": 0, "error": 0, "running": 0}
    for r in runs:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    series = {
        cid: {"latest": None, "latest_skipped": None, "min": None, "max": None, "measured_days": 0, "breach_days": 0}
        for cid in METRIC_IDS
    }
    if runs:
        results = EvalResult.objects.filter(run__in=runs).values("case_id", "passed", "score", "run_id", "details")
        # Group by metric, preserving run order via a run_id → position map.
        order = {r.id: i for i, r in enumerate(runs)}
        by_metric: dict[str, list] = {cid: [] for cid in METRIC_IDS}
        for row in results:
            if row["case_id"] in by_metric:
                by_metric[row["case_id"]].append(row)
        for cid, rows in by_metric.items():
            rows.sort(key=lambda x: order.get(x["run_id"], 0))
            measured = [r for r in rows if r["score"] is not None and not r["details"].get("skipped")]
            series[cid]["measured_days"] = len(measured)
            values = [float(r["score"]) for r in measured]
            if values:
                series[cid]["min"] = min(values)
                series[cid]["max"] = max(values)
            if rows:
                last = rows[-1]
                skipped = bool(last["details"].get("skipped"))
                series[cid]["latest_skipped"] = skipped
                series[cid]["latest"] = None if skipped else float(last["score"])
            series[cid]["breach_days"] = sum(1 for r in rows if not r["passed"])
    return len(runs), status_counts, series


def _fmt(value) -> str:
    """Compact number formatting for the digest table (int-ish shows no decimals)."""
    if value is None:
        return "-"
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def build_weekly_digest(now=None) -> tuple[str, str]:
    """Render the trailing-7-day SLO digest as ``(subject, plain_text_body)``.

    A weekly READOUT, not an alarm: it renders and is sent even when every day was
    green (the send lives in the task). Zero snapshots in the window is itself
    surfaced as a finding (the nightly job is not running). Body carries only
    metric ids, thresholds, and numbers — content-free by construction.
    """
    now = now or timezone.now()
    thr = thresholds()
    snapshot_count, status_counts, series = _metric_series(now)

    ending = now.strftime("%Y-%m-%d")
    lines = [
        f"NBHD SLO weekly digest — 7 days ending {ending} UTC",
        "",
        f"Snapshots recorded: {snapshot_count} "
        f"(pass={status_counts['pass']} fail={status_counts['fail']} error={status_counts['error']})",
        "",
    ]

    if snapshot_count == 0:
        lines += [
            "No slo_snapshot runs in the last 7 days.",
            "",
            "That is itself a finding: the nightly snapshot task is not firing. "
            "Confirm the QStash schedule for 'slo_snapshot' exists and is enabled.",
        ]
        return "[SLO digest] no snapshots in 7 days — nightly job not firing?", "\n".join(lines)

    header = f"{'metric':<28}{'thresh':>10}{'latest':>10}{'min':>10}{'max':>10}{'meas':>8}{'breach':>8}"
    lines.append(header)
    lines.append("-" * len(header))
    total_breach_days = 0
    for cid in METRIC_IDS:
        s = series[cid]
        total_breach_days += s["breach_days"]
        bound = ">=" if cid in _FLOOR_METRICS else "<="
        thresh_col = f"{bound}{_fmt(thr[cid])}"
        if s["latest"] is None and s["latest_skipped"]:
            latest_col = "skip"
        else:
            latest_col = _fmt(s["latest"])
        # measured-days out of snapshots: an all-week empty window reads 0/N here,
        # not just one 'skip' in the latest column (digest honesty — a metric that
        # was never measurable all week is its own finding).
        meas_col = f"{s['measured_days']}/{snapshot_count}"
        lines.append(
            f"{cid:<28}{thresh_col:>10}{latest_col:>10}{_fmt(s['min']):>10}{_fmt(s['max']):>10}"
            f"{meas_col:>8}{s['breach_days']:>8}"
        )

    lines += [
        "-" * len(header),
        "",
        f"Total breach-metric-days in the last 7: {total_breach_days}.",
        "'skip' = no data, OR not enough of it to measure honestly (reason + n in details).",
        "'meas' = snapshots that actually measured the metric; 0/N means it was never measurable all week.",
        "Thresholds are settings.EVAL_SLO_THRESHOLDS (code defaults otherwise); latencies in ms.",
        "Detail: EvalRun/EvalResult rows for suite 'slo_snapshot'. Metadata only — no message content.",
    ]
    subject = f"[SLO digest] {snapshot_count} snapshots, {total_breach_days} breach-days (7d)"
    return subject, "\n".join(lines)
