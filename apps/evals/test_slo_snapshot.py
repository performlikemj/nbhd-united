"""Tests for Suite 4 — the production SLO snapshot + weekly digest.

Covers the load-bearing properties (docs/evals-directive.md §Suite 4):
  * percentile math (type-7 interpolation);
  * synthetic-tenant exclusion — a synthetic tenant's slow turn must NOT move p95
    (and the SAME exclusion independently on the error-rate query);
  * a threshold breach closes the run FAIL (the breach-flag mechanism), in BOTH
    directions (ceiling and floor);
  * an empty window is skipped-with-reason, never a passing zero;
  * the journey-canary budget-cap saturation tripwire (a fully-capped canary that
    still soft-passes must NOT read healthy; per-probe denominators — one probe's
    healthy runs must not dilute another's saturation) plus the rename-blindness
    guard pinning the tripwire's markers to the journey suites' own constants;
  * threshold overrides via settings (honored for known keys, warned for unknown);
  * the weekly digest renders (incl. the measured-days honesty column) and gates
    on the owner email being set.

All fixtures are metadata: timestamps, statuses, counts. No message body is ever
read by the code under test.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.evals.models import EvalResult, EvalRun
from apps.evals.suites.slo_snapshot import (
    _BUDGET_EXHAUSTED_MARKER,
    DEFAULT_SLO_THRESHOLDS,
    JOURNEY_PROBE_SUITES,
    M_ERROR_RATE,
    M_EVAL_RUN_ERRORS,
    M_JOURNEY_BUDGET_CAPPED,
    M_PROACTIVE_DELIVERIES,
    M_REPLY_P50,
    M_REPLY_P95,
    M_WAKE_P95,
    MIN_SAMPLE_P50,
    MIN_SAMPLE_P95,
    build_weekly_digest,
    compute_error_rate,
    compute_reply_latency,
    percentile,
    run_slo_snapshot_suite,
    thresholds,
)
from apps.router.models import AppChatMessage, ChatThread, ProactiveOutbound
from apps.tenants.models import Tenant, User


def _make_tenant(*, synthetic: bool = False) -> Tenant:
    user = User.objects.create_user(
        username=f"slo_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-slo.example.com",
        is_synthetic=synthetic,
    )


def _proactive_row(tenant, *, created_at) -> ProactiveOutbound:
    row = ProactiveOutbound.objects.create(
        tenant=tenant,
        channel=ProactiveOutbound.Channel.APP,
        channel_user_id="u1",
        message_text="[PERSON_1] ping",
    )
    ProactiveOutbound.objects.filter(pk=row.pk).update(created_at=created_at)
    return row


class _ChatFixtureMixin:
    def _thread(self, tenant) -> ChatThread:
        """One is_main thread per tenant (a UniqueConstraint forbids two)."""
        cache = self.__dict__.setdefault("_threads", {})
        thread = cache.get(tenant.id)
        if thread is None:
            thread = ChatThread.objects.create(tenant=tenant, user=tenant.user, is_main=True, title="Main")
            cache[tenant.id] = thread
        return thread

    def _msg(
        self,
        tenant,
        *,
        created,
        replied=None,
        status=AppChatMessage.Status.READY,
        source=AppChatMessage.Source.TENANT,
        waking=None,
        error="",
    ) -> AppChatMessage:
        """Create a chat turn with a backdated ``created_at`` (auto_now_add needs .update())."""
        thread = self._thread(tenant)
        m = AppChatMessage.objects.create(
            tenant=tenant,
            user=tenant.user,
            thread=thread,
            client_msg_id=secrets.token_hex(6),
            user_text="q",  # a body — the code under test must never read it
            reply_text="a",
            status=status,
            source=source,
            replied_at=replied,
            waking_at=waking,
            error=error,
        )
        AppChatMessage.objects.filter(pk=m.pk).update(created_at=created)
        return m


class RenameBlindnessGuardTest(TestCase):
    """Pin the budget-cap tripwire's markers to the journey suites' OWN constants.

    ``compute_journey_budget_capped`` matches ``EvalRun.suite`` strings and the
    ``details.outcome`` marker by value. If a journey suite renamed its SUITE or
    its BUDGET_EXHAUSTED code, the tripwire would silently match nothing — every
    ratio 0.0, every test still green. These equality assertions make that rename
    break THIS test instead.
    """

    def test_budget_marker_matches_both_journey_suites(self):
        from apps.evals.suites.journey_chat import ChatOutcome
        from apps.evals.suites.journey_wake import WakeOutcome

        self.assertEqual(_BUDGET_EXHAUSTED_MARKER, ChatOutcome.BUDGET_EXHAUSTED)
        self.assertEqual(_BUDGET_EXHAUSTED_MARKER, WakeOutcome.BUDGET_EXHAUSTED)

    def test_probe_suite_names_match_journey_suite_constants(self):
        from apps.evals.suites import journey_chat, journey_wake

        self.assertEqual(JOURNEY_PROBE_SUITES, (journey_chat.SUITE, journey_wake.SUITE))


class PercentileMathTest(TestCase):
    def test_type7_interpolation_and_edges(self):
        vals = [10, 20, 30, 40, 50]
        # p50: rank = 0.5*(5-1) = 2 → vals[2] = 30.
        self.assertEqual(percentile(vals, 50), 30.0)
        # p95: rank = 0.95*4 = 3.8 → 40 + 0.8*(50-40) = 48.
        self.assertAlmostEqual(percentile(vals, 95), 48.0)
        # p100 clamps to the max; p0 to the min.
        self.assertEqual(percentile(vals, 100), 50.0)
        self.assertEqual(percentile(vals, 0), 10.0)

    def test_empty_is_none_not_zero(self):
        # The whole point: no data must never read as a perfect 0ms latency.
        self.assertIsNone(percentile([], 95))

    def test_single_value(self):
        self.assertEqual(percentile([7], 95), 7.0)


class ReplyLatencySyntheticExclusionTest(_ChatFixtureMixin, TestCase):
    def test_synthetic_slow_turn_does_not_move_p95(self):
        now = timezone.now()
        real = _make_tenant(synthetic=False)
        synth = _make_tenant(synthetic=True)

        created = now - timedelta(hours=1)
        # 12 fast REAL turns, all ~1000ms.
        for _ in range(12):
            self._msg(real, created=created, replied=created + timedelta(milliseconds=1000))
        # One monstrously slow SYNTHETIC turn (10 minutes) that WOULD dominate p95
        # if it were counted. It must be excluded.
        self._msg(synth, created=created, replied=created + timedelta(milliseconds=600_000))

        result = compute_reply_latency(now)
        self.assertIsNotNone(result)
        # Only the 12 real turns counted — the synthetic one is invisible.
        self.assertEqual(result["n"], 12)
        # p95 reflects the real ~1000ms population, nowhere near the 600s synthetic.
        self.assertLess(result["p95"], 2000)


class WarmOnlyReplyLatencyTest(_ChatFixtureMixin, TestCase):
    """A woken turn was being judged TWICE, by two ceilings that disagree.

    ``compute_wake_latency_p95`` already measures cold starts against a deliberately
    higher ceiling (90s) *because* the wake path is the slow one. Counting them in the
    warm reply SLO too meant the same turn passed one metric and breached the other.
    Prod 2026-07-13: a 95.4s turn passed the wake SLO (80.5s of it was wake, under 90s)
    while breaching the 45s reply ceiling, and the p50 "breach" (16,198ms vs 15,000)
    was ENTIRELY the two cold starts — warm-only it was 14,812ms. Green.
    """

    def test_woken_turns_are_excluded_from_the_warm_reply_slo(self):
        now = timezone.now()
        real = _make_tenant(synthetic=False)
        created = now - timedelta(hours=1)

        # Ten warm turns, all ~1s.
        for _ in range(10):
            self._msg(real, created=created, replied=created + timedelta(milliseconds=1000))
        # One cold start: a 90s wait, the overwhelming majority of it spent waking a
        # hibernated container. It would dominate the warm p95 if it were counted.
        self._msg(
            real,
            created=created,
            waking=created + timedelta(milliseconds=2_000),
            replied=created + timedelta(milliseconds=90_000),
        )

        result = compute_reply_latency(now)
        self.assertIsNotNone(result)
        self.assertEqual(result["n"], 10, "the woken turn leaked into the warm sample")
        self.assertLess(result["p95"], 2000, "the cold start is dominating a WARM latency metric")
        # …but it is NOT dropped on the floor. The count is carried so a reader can
        # never mistake a thin warm sample for a quiet fleet.
        self.assertEqual(result["n_woken"], 1)


class InsufficientSampleIsSkippedNotScoredTest(_ChatFixtureMixin, TestCase):
    """A "95th percentile" of a handful of turns is not a percentile.

    Prod 2026-07-13: the entire 24h window held 16 real turns across the whole fleet.
    At n=16 the type-7 p95 interpolates between the 15th and 16th sorted values — the
    second-slowest turn of the day wearing a statistical costume. It breaches whenever
    anyone waits, and a metric that always breaches gets ignored, which is worse than
    not having it. Below the floor we say so, with the actual n, rather than publish a
    number we cannot stand behind.
    """

    def test_a_thin_sample_cannot_manufacture_a_p95_breach(self):
        now = timezone.now()
        real = _make_tenant(synthetic=False)
        created = now - timedelta(hours=1)
        n = MIN_SAMPLE_P95 - 1  # enough for a median, one short of a tail
        for _ in range(n):
            self._msg(real, created=created, replied=created + timedelta(milliseconds=60_000))

        run = run_slo_snapshot_suite(now=now)
        run.refresh_from_db()

        # THE POINT: 60s turns on a sample this thin must not produce a "p95 breach".
        # A skip never gates, so whatever the run does, it does not do it because of p95.
        p95 = run.results.get(case_id=M_REPLY_P95)
        self.assertTrue(p95.details.get("skipped"))
        self.assertIsNone(p95.score)
        self.assertEqual(p95.details.get("reason"), "insufficient_sample")
        self.assertEqual(p95.details.get("n"), n)
        self.assertEqual(p95.details.get("floor"), MIN_SAMPLE_P95)
        self.assertTrue(p95.passed)

        # The MEDIAN, however, is honestly measurable at this n — and 60s turns really
        # do breach a 15s median SLO. The run fails on p50, which is a true finding.
        # Suppressing that too would be the opposite mistake.
        p50 = run.results.get(case_id=M_REPLY_P50)
        self.assertFalse(p50.details.get("skipped"))
        self.assertFalse(p50.passed)
        self.assertEqual(run.status, EvalRun.Status.FAIL)

    def test_the_median_survives_a_window_the_p95_cannot(self):
        """The floors are per-metric on purpose: a median is far more forgiving than a
        tail, so a thin window must not drag the p50 down with the p95."""
        now = timezone.now()
        real = _make_tenant(synthetic=False)
        created = now - timedelta(hours=1)
        n = MIN_SAMPLE_P50  # enough for a median, NOT enough for a 95th percentile
        self.assertLess(n, MIN_SAMPLE_P95)
        for _ in range(n):
            self._msg(real, created=created, replied=created + timedelta(milliseconds=1000))

        run = run_slo_snapshot_suite(now=now)
        run.refresh_from_db()

        p50 = run.results.get(case_id=M_REPLY_P50)
        p95 = run.results.get(case_id=M_REPLY_P95)
        self.assertFalse(p50.details.get("skipped"), "the median was thrown away with the tail")
        self.assertIsNotNone(p50.score)
        self.assertTrue(p95.details.get("skipped"))


class WakeLatencyTest(_ChatFixtureMixin, TestCase):
    def test_wake_latency_measured_from_waking_to_replied(self):
        from apps.evals.suites.slo_snapshot import compute_wake_latency_p95

        now = timezone.now()
        real = _make_tenant(synthetic=False)
        synth = _make_tenant(synthetic=True)
        created = now - timedelta(minutes=30)
        # Real wake turn: waking 2s after created, replied 5s after waking.
        waking = created + timedelta(seconds=2)
        self._msg(real, created=created, waking=waking, replied=waking + timedelta(seconds=5))
        # A synthetic wake turn with a huge delay — must be excluded.
        self._msg(
            synth,
            created=created,
            waking=created + timedelta(seconds=1),
            replied=created + timedelta(seconds=400),
        )
        # A real turn with NO waking_at (warm path) — not a wake sample.
        self._msg(real, created=created, replied=created + timedelta(seconds=1))

        result = compute_wake_latency_p95(now)
        self.assertIsNotNone(result)
        self.assertEqual(result["n"], 1)  # only the one real wake turn
        self.assertAlmostEqual(result["p95"], 5000.0, delta=1.0)


class ErrorRateTest(_ChatFixtureMixin, TestCase):
    def test_rate_math_pending_excluded_synthetic_excluded(self):
        now = timezone.now()
        real = _make_tenant(synthetic=False)
        synth = _make_tenant(synthetic=True)
        created = now - timedelta(hours=1)

        # 3 ready + 1 error REAL finished turns → rate 1/4 = 0.25.
        for _ in range(3):
            self._msg(real, created=created, replied=created + timedelta(seconds=1))
        self._msg(
            real,
            created=created,
            replied=created + timedelta(seconds=1),
            status=AppChatMessage.Status.ERROR,
            error="empty_response",
        )
        # A still-pending real turn must NOT enter the denominator (it is not a
        # failure yet — counting it would dilute the rate).
        self._msg(real, created=created, status=AppChatMessage.Status.PENDING)
        # 5 SYNTHETIC error turns that would swing the rate to 6/9 if THIS query
        # lost its own tenant__is_synthetic=False filter — pinned independently of
        # the latency queries' exclusion.
        for _ in range(5):
            self._msg(
                synth,
                created=created,
                replied=created + timedelta(seconds=1),
                status=AppChatMessage.Status.ERROR,
                error="empty_response",
            )

        result = compute_error_rate(now)
        self.assertIsNotNone(result)
        self.assertEqual(result["total"], 4)  # 3 ready + 1 error; pending + synthetic out
        self.assertEqual(result["errors"], 1)
        self.assertAlmostEqual(result["rate"], 0.25)

    def test_error_rate_breach_flags_metric_through_suite(self):
        now = timezone.now()
        real = _make_tenant(synthetic=False)
        created = now - timedelta(hours=1)
        # 1 error of 2 finished turns → 0.5, far over the 0.05 default ceiling.
        self._msg(real, created=created, replied=created + timedelta(seconds=1))
        self._msg(
            real,
            created=created,
            replied=created + timedelta(seconds=1),
            status=AppChatMessage.Status.ERROR,
            error="empty_response",
        )

        run = run_slo_snapshot_suite(now=now)
        metric = run.results.get(case_id=M_ERROR_RATE)
        self.assertAlmostEqual(float(metric.score), 0.5)
        self.assertFalse(metric.passed)
        run.refresh_from_db()
        self.assertEqual(run.status, EvalRun.Status.FAIL)


class BreachClosesRunFailTest(_ChatFixtureMixin, TestCase):
    def test_p95_breach_closes_run_fail_and_flags_metric(self):
        now = timezone.now()
        real = _make_tenant(synthetic=False)
        created = now - timedelta(hours=1)
        # ``MIN_SAMPLE_P95`` warm turns, each ~60s — over the 45s p95 SLO → breach.
        #
        # This used to seed FIVE turns and assert a p95 breach, which is the very bug
        # the sample floor exists to stop: a "95th percentile" of five observations is
        # not a percentile. The floor is IMPORTED, not re-pinned as a literal, so this
        # test tracks it instead of silently going vacuous the next time it moves.
        for _ in range(MIN_SAMPLE_P95):
            self._msg(real, created=created, replied=created + timedelta(milliseconds=60_000))

        run = run_slo_snapshot_suite(now=now)

        run.refresh_from_db()
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        p95 = run.results.get(case_id=M_REPLY_P95)
        self.assertFalse(p95.passed)
        self.assertGreater(float(p95.score), float(thresholds()[M_REPLY_P95]))
        # A breached metric carries a real score + threshold (not a skip).
        self.assertFalse(p95.details.get("skipped"))


class EmptyWindowSkippedNotGreenTest(TestCase):
    def test_latency_metrics_skip_with_reason_never_passing_zero(self):
        now = timezone.now()
        # No chat traffic at all in the window.
        run = run_slo_snapshot_suite(now=now)

        run.refresh_from_db()
        # Latency/rate metrics: recorded, but as SKIPPED — score is None, not 0.
        for cid, reason in (
            (M_REPLY_P50, "no_warm_ready_turns_24h"),
            (M_REPLY_P95, "no_warm_ready_turns_24h"),
            (M_WAKE_P95, "no_wake_turns_24h"),
            (M_ERROR_RATE, "no_finished_turns_24h"),
            (M_JOURNEY_BUDGET_CAPPED, "no_journey_runs_24h"),
        ):
            r = run.results.get(case_id=cid)
            self.assertIsNone(r.score, f"{cid} must skip with score=None, not a passing zero")
            self.assertTrue(r.details.get("skipped"), f"{cid} must be flagged skipped")
            self.assertEqual(r.details.get("reason"), reason)
            self.assertTrue(r.passed, f"{cid} skip is not, by itself, a breach")

        # Count metrics still produce a real 0 (a genuine measurement, floor-checked).
        deliveries = run.results.get(case_id=M_PROACTIVE_DELIVERIES)
        self.assertEqual(float(deliveries.score), 0.0)
        self.assertFalse(deliveries.details.get("skipped"))
        self.assertTrue(deliveries.passed)

        # With only count-metrics measured (all healthy) and the rest skipped, the
        # run is a pass — an empty window is not, on its own, a failure.
        self.assertEqual(run.status, EvalRun.Status.PASS)


class ProactiveDeliverySyntheticExclusionTest(TestCase):
    def test_only_real_tenant_deliveries_counted(self):
        now = timezone.now()
        real = _make_tenant(synthetic=False)
        synth = _make_tenant(synthetic=True)
        for t, n in ((real, 3), (synth, 5)):
            for _ in range(n):
                _proactive_row(t, created_at=now - timedelta(hours=1))

        run = run_slo_snapshot_suite(now=now)
        deliveries = run.results.get(case_id=M_PROACTIVE_DELIVERIES)
        # 3 real deliveries; the 5 synthetic ones excluded.
        self.assertEqual(float(deliveries.score), 3.0)


class ThresholdOverrideTest(TestCase):
    @override_settings(EVAL_SLO_THRESHOLDS={M_REPLY_P95: 30000})
    def test_known_key_override_honored_others_default(self):
        thr = thresholds()
        self.assertEqual(thr[M_REPLY_P95], 30000)
        # Untouched keys keep their code defaults.
        self.assertEqual(thr[M_REPLY_P50], DEFAULT_SLO_THRESHOLDS[M_REPLY_P50])

    @override_settings(EVAL_SLO_THRESHOLDS={"reply_latency_p95_msec": 1})
    def test_unknown_key_is_warned_and_dropped(self):
        with self.assertLogs("apps.evals.suites.slo_snapshot", level="WARNING") as log:
            thr = thresholds()
        # The typo'd key never becomes a phantom metric, and the default survives.
        self.assertNotIn("reply_latency_p95_msec", thr)
        self.assertEqual(thr[M_REPLY_P95], DEFAULT_SLO_THRESHOLDS[M_REPLY_P95])
        self.assertTrue(any("reply_latency_p95_msec" in m for m in log.output))

    @override_settings(EVAL_SLO_THRESHOLDS={M_PROACTIVE_DELIVERIES: 5})
    def test_floor_direction_breaches_below_floor(self):
        """3 deliveries under a floor of 5 MUST breach — pins the floor branch.

        With the shipped floor of 0 and a 0 count, an inverted floor comparison
        (``>`` instead of ``<``) would still pass every other test; this is the
        only case that distinguishes the directions.
        """
        now = timezone.now()
        real = _make_tenant(synthetic=False)
        for _ in range(3):
            _proactive_row(real, created_at=now - timedelta(hours=1))

        run = run_slo_snapshot_suite(now=now)
        metric = run.results.get(case_id=M_PROACTIVE_DELIVERIES)
        self.assertEqual(float(metric.score), 3.0)
        self.assertFalse(metric.passed)  # 3 < floor 5 → breach
        run.refresh_from_db()
        self.assertEqual(run.status, EvalRun.Status.FAIL)


class EvalRunErrorMetricTest(TestCase):
    def test_error_and_stranded_runs_counted_fail_is_not(self):
        now = timezone.now()
        # An error-closed run in the window → counted. Backdate started_at into the
        # window (auto_now_add stamps 'now-ish', which can land just after the
        # captured `now` and fall outside the [now-24h, now] bound).
        err = EvalRun.objects.create(suite="journey_chat", trigger=EvalRun.Trigger.MANUAL, status=EvalRun.Status.ERROR)
        EvalRun.objects.filter(pk=err.pk).update(started_at=now - timedelta(hours=1))
        # A fail-closed run → NOT counted (a fail is the system correctly catching a break).
        fail = EvalRun.objects.create(suite="journey_chat", trigger=EvalRun.Trigger.MANUAL, status=EvalRun.Status.FAIL)
        EvalRun.objects.filter(pk=fail.pk).update(started_at=now - timedelta(hours=1))
        # A run stuck 'running' for > 30min → counted as stranded.
        stranded = EvalRun.objects.create(
            suite="journey_wake", trigger=EvalRun.Trigger.MANUAL, status=EvalRun.Status.RUNNING
        )
        EvalRun.objects.filter(pk=stranded.pk).update(started_at=now - timedelta(minutes=45))

        run = run_slo_snapshot_suite(now=now)
        metric = run.results.get(case_id=M_EVAL_RUN_ERRORS)
        # 1 error + 1 stranded = 2; the fail run is excluded.
        self.assertEqual(float(metric.score), 2.0)
        self.assertFalse(metric.passed)  # threshold 0 → any is a breach
        run.refresh_from_db()
        self.assertEqual(run.status, EvalRun.Status.FAIL)


class JourneyBudgetCappedMetricTest(TestCase):
    def _journey_run(self, suite: str, *, capped: bool, now):
        run = EvalRun.objects.create(suite=suite, trigger=EvalRun.Trigger.MANUAL, status=EvalRun.Status.PASS)
        EvalRun.objects.filter(pk=run.pk).update(started_at=now - timedelta(hours=2), finished_at=now)
        if capped:
            EvalResult.objects.create(
                run=run,
                case_id=f"{suite}_budget_capped",
                kind=EvalResult.Kind.JOURNEY,
                passed=True,
                details={"outcome": _BUDGET_EXHAUSTED_MARKER},
            )
        else:
            EvalResult.objects.create(
                run=run,
                case_id="roundtrip",
                kind=EvalResult.Kind.JOURNEY,
                passed=True,
                details={"outcome": "pass"},
            )
        return run

    def test_majority_capped_is_a_breach(self):
        now = timezone.now()
        # 2 of 3 journey_chat runs were budget-capped soft passes → 0.667 > 0.5.
        self._journey_run("journey_chat", capped=True, now=now)
        self._journey_run("journey_chat", capped=True, now=now)
        self._journey_run("journey_chat", capped=False, now=now)

        run = run_slo_snapshot_suite(now=now)
        metric = run.results.get(case_id=M_JOURNEY_BUDGET_CAPPED)
        self.assertAlmostEqual(float(metric.score), 0.667, places=2)
        self.assertFalse(metric.passed)
        self.assertEqual(metric.details.get("worst_probe"), "journey_chat")
        run.refresh_from_db()
        self.assertEqual(run.status, EvalRun.Status.FAIL)

    def test_fully_tripped_single_run_does_not_look_healthy(self):
        now = timezone.now()
        # The exact hazard: the canary fired once and it was capped → ratio 1.0.
        self._journey_run("journey_wake", capped=True, now=now)

        run = run_slo_snapshot_suite(now=now)
        metric = run.results.get(case_id=M_JOURNEY_BUDGET_CAPPED)
        self.assertEqual(float(metric.score), 1.0)
        self.assertFalse(metric.passed)
        self.assertEqual(metric.details.get("worst_probe"), "journey_wake")

    def test_one_capped_probe_not_diluted_by_other_probes_health(self):
        """Anti-dilution: per-probe denominators, never a pooled one.

        3 healthy journey_chat runs + 1 budget-capped journey_wake run. A pooled
        denominator would read 1/4 = 0.25 and pass; the correct per-probe view is
        wake at 1/1 = 1.0 — a fully-capped wake canary — which MUST breach.
        """
        now = timezone.now()
        self._journey_run("journey_chat", capped=False, now=now)
        self._journey_run("journey_chat", capped=False, now=now)
        self._journey_run("journey_chat", capped=False, now=now)
        self._journey_run("journey_wake", capped=True, now=now)

        run = run_slo_snapshot_suite(now=now)
        metric = run.results.get(case_id=M_JOURNEY_BUDGET_CAPPED)
        self.assertEqual(float(metric.score), 1.0)
        self.assertFalse(metric.passed)
        self.assertEqual(metric.details.get("worst_probe"), "journey_wake")
        self.assertEqual(metric.details.get("wake_soft"), 1)
        self.assertEqual(metric.details.get("wake_total"), 1)
        self.assertEqual(metric.details.get("chat_soft"), 0)
        self.assertEqual(metric.details.get("chat_total"), 3)
        run.refresh_from_db()
        self.assertEqual(run.status, EvalRun.Status.FAIL)

    def test_minority_capped_passes(self):
        now = timezone.now()
        # 1 of 3 capped → 0.333, below the 0.5 majority threshold → healthy.
        self._journey_run("journey_chat", capped=True, now=now)
        self._journey_run("journey_chat", capped=False, now=now)
        self._journey_run("journey_chat", capped=False, now=now)

        run = run_slo_snapshot_suite(now=now)
        metric = run.results.get(case_id=M_JOURNEY_BUDGET_CAPPED)
        self.assertAlmostEqual(float(metric.score), 0.333, places=2)
        self.assertTrue(metric.passed)


class WeeklyDigestTest(TestCase):
    def _snapshot(self, now):
        return run_slo_snapshot_suite(now=now)

    def test_digest_renders_metric_table_with_measured_days(self):
        # Two snapshots this week (their started_at is auto_now_add ≈ real now).
        # Neither has chat traffic, so latency metrics skip in BOTH.
        self._snapshot(timezone.now() - timedelta(days=2))
        self._snapshot(timezone.now())
        # Capture the digest window's `now` AFTER creating them so their started_at
        # falls at/below the upper bound (in production `now` is captured at run).
        now = timezone.now()

        subject, body = build_weekly_digest(now=now)
        self.assertIn("SLO", subject)
        self.assertIn("weekly digest", body)
        # Every metric row is present.
        for cid in (
            M_REPLY_P50,
            M_REPLY_P95,
            M_WAKE_P95,
            M_ERROR_RATE,
            M_PROACTIVE_DELIVERIES,
            M_EVAL_RUN_ERRORS,
        ):
            self.assertIn(cid, body)
        # Measured-days honesty column: a metric skipped ALL week reads 0/2 on its
        # row — not hidden behind a single 'skip' in the latest column — while an
        # always-measured count metric reads 2/2.
        self.assertIn("meas", body)
        lines = body.splitlines()
        wake_line = next(ln for ln in lines if ln.startswith(M_WAKE_P95))
        self.assertIn("0/2", wake_line)
        deliveries_line = next(ln for ln in lines if ln.startswith(M_PROACTIVE_DELIVERIES))
        self.assertIn("2/2", deliveries_line)

    def test_digest_with_no_snapshots_is_itself_a_finding(self):
        now = timezone.now()
        subject, body = build_weekly_digest(now=now)
        self.assertIn("no snapshots", subject.lower())
        self.assertIn("not firing", body.lower())


class WeeklyDigestTaskGateTest(TestCase):
    def setUp(self):
        # A snapshot so the digest has something to render.
        run_slo_snapshot_suite(now=timezone.now())

    @override_settings(PLATFORM_OWNER_EMAIL="owner@example.com")
    def test_sends_when_owner_email_set(self):
        from apps.evals.tasks import weekly_slo_digest_task

        mail.outbox = []
        result = weekly_slo_digest_task()
        self.assertTrue(result["sent"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("SLO", mail.outbox[0].subject)

    @override_settings(PLATFORM_OWNER_EMAIL="")
    def test_gates_and_skips_when_owner_email_unset(self):
        from apps.evals.tasks import weekly_slo_digest_task

        mail.outbox = []
        result = weekly_slo_digest_task()
        self.assertFalse(result["sent"])
        self.assertEqual(len(mail.outbox), 0)  # gated — no send, and never raises
