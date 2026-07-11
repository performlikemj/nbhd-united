"""Tests for Suite 4 — the production SLO snapshot + weekly digest.

Covers the load-bearing properties (docs/evals-directive.md §Suite 4):
  * percentile math (type-7 interpolation);
  * synthetic-tenant exclusion — a synthetic tenant's slow turn must NOT move p95;
  * a threshold breach closes the run FAIL (the breach-flag mechanism);
  * an empty window is skipped-with-reason, never a passing zero;
  * the journey-canary budget-cap saturation tripwire (a fully-capped canary that
    still soft-passes must NOT read healthy);
  * the weekly digest renders and gates on the owner email being set.

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
    M_CRON_DELIVERIES,
    M_ERROR_RATE,
    M_EVAL_RUN_ERRORS,
    M_JOURNEY_BUDGET_CAPPED,
    M_REPLY_P50,
    M_REPLY_P95,
    M_WAKE_P95,
    build_weekly_digest,
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


class BreachClosesRunFailTest(_ChatFixtureMixin, TestCase):
    def test_p95_breach_closes_run_fail_and_flags_metric(self):
        now = timezone.now()
        real = _make_tenant(synthetic=False)
        created = now - timedelta(hours=1)
        # Five real turns each ~60s — well over the 45s p95 SLO → breach.
        for _ in range(5):
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
            (M_REPLY_P50, "no_ready_turns_24h"),
            (M_REPLY_P95, "no_ready_turns_24h"),
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
        cron = run.results.get(case_id=M_CRON_DELIVERIES)
        self.assertEqual(float(cron.score), 0.0)
        self.assertFalse(cron.details.get("skipped"))
        self.assertTrue(cron.passed)

        # With only count-metrics measured (all healthy) and the rest skipped, the
        # run is a pass — an empty window is not, on its own, a failure.
        self.assertEqual(run.status, EvalRun.Status.PASS)


class CronDeliverySyntheticExclusionTest(TestCase):
    def test_only_real_tenant_deliveries_counted(self):
        now = timezone.now()
        real = _make_tenant(synthetic=False)
        synth = _make_tenant(synthetic=True)
        for t, n in ((real, 3), (synth, 5)):
            for _ in range(n):
                row = ProactiveOutbound.objects.create(
                    tenant=t,
                    channel=ProactiveOutbound.Channel.APP,
                    channel_user_id="u1",
                    message_text="[PERSON_1] ping",
                )
                ProactiveOutbound.objects.filter(pk=row.pk).update(created_at=now - timedelta(hours=1))

        run = run_slo_snapshot_suite(now=now)
        cron = run.results.get(case_id=M_CRON_DELIVERIES)
        # 3 real deliveries; the 5 synthetic ones excluded.
        self.assertEqual(float(cron.score), 3.0)


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
                details={"outcome": "budget_exhausted"},
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

    def test_digest_renders_metric_table(self):
        # Two snapshots this week (their started_at is auto_now_add ≈ real now).
        self._snapshot(timezone.now() - timedelta(days=2))
        self._snapshot(timezone.now())
        # Capture the digest window's `now` AFTER creating them so their started_at
        # falls at/below the upper bound (in production `now` is captured at run).
        now = timezone.now()

        subject, body = build_weekly_digest(now=now)
        self.assertIn("SLO", subject)
        self.assertIn("weekly digest", body)
        # Every metric row is present.
        for cid in (M_REPLY_P50, M_REPLY_P95, M_WAKE_P95, M_ERROR_RATE, M_CRON_DELIVERIES, M_EVAL_RUN_ERRORS):
            self.assertIn(cid, body)

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
