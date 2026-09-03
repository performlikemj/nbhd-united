from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.evals.models import EvalResult, EvalRun
from apps.steward.digest import (
    MAX_DIGEST_CHARS,
    render_steward_daily_digest,
    run_steward_daily_digest,
)
from apps.steward.models import (
    CollectorStatus,
    DigestRecord,
    EvidenceEvent,
    EvidenceSource,
    Expectation,
    TrackedItem,
)


class StewardDigestTests(TestCase):
    def _item(
        self,
        title: str,
        *,
        status=TrackedItem.Status.ACTIVE,
        kind=TrackedItem.Kind.WORK,
        age_days=0,
        context="",
    ) -> TrackedItem:
        item = TrackedItem.objects.create(
            product=TrackedItem.Product.PORTFOLIO,
            kind=kind,
            title=title,
            context=context,
            status=status,
            provenance=EvidenceEvent.Provenance.MJ,
        )
        TrackedItem.objects.filter(pk=item.pk).update(status_changed_at=timezone.now() - timedelta(days=age_days))
        item.refresh_from_db()
        return item

    def _missed(self, subject: str, *, urgent=False) -> Expectation:
        now = timezone.now()
        return Expectation.objects.create(
            kind=Expectation.Kind.DEADLINE,
            due_at=now - timedelta(days=2),
            grace_s=3600,
            evidence_source=EvidenceSource.MJ_ACK,
            subject=subject,
            state=Expectation.State.MISSED,
            last_alerted_at=now - timedelta(hours=3) if urgent else None,
            on_miss=(Expectation.OnMiss.URGENT if urgent else Expectation.OnMiss.DIGEST),
        )

    def _slo_breach(self):
        now = timezone.now()
        run = EvalRun.objects.create(
            suite="slo_snapshot",
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.FAIL,
            finished_at=now,
        )
        EvalRun.objects.filter(pk=run.pk).update(started_at=now)
        EvalResult.objects.create(
            run=run,
            case_id="reply_latency_p50_ms",
            kind=EvalResult.Kind.SLO,
            passed=False,
            score=Decimal("40.000"),
            threshold=Decimal("15.000"),
            details={"poison": "NEVER_RENDER_DETAILS"},
        )
        return run

    def test_renders_actionable_sections_without_changes_or_run_history(self):
        now = timezone.now()
        self._item(
            "Needs decision",
            status=TrackedItem.Status.BLOCKED,
            age_days=2,
            context="MJ-authored context",
        )
        self._missed("deadline-one", urgent=True)
        self._slo_breach()
        self._item("No expectation")
        EvidenceEvent.objects.create(
            source=EvidenceSource.EVAL_RUN,
            subject="eval:journey",
            occurred_at=now,
            payload={
                "run_id": 123,
                "status": EvalRun.Status.PASS,
                "prev_status_at_collection": EvalRun.Status.FAIL,
                "passed": 1,
                "total": 1,
                "git_sha": "abc123",
                "poison": "NEVER_RENDER_PAYLOAD",
            },
            fingerprint="digest-transition",
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )

        text, stats = render_steward_daily_digest(now=now + timedelta(minutes=1))

        for heading in (
            "NEEDS YOU",
            "STALLED",
            "SLO / EVALS",
            "INTEGRITY",
        ):
            self.assertIn(heading, text)
        self.assertIn("Needs decision", text)
        self.assertIn("deadline-one", text)
        self.assertIn("reply_latency_p50_ms", text)
        self.assertIn("40.000 vs 15.000 (1 breach days) — inspect latest slo_snapshot run", text)
        self.assertNotIn("journey: run 123 finished pass", text)
        self.assertNotIn("CHANGES (24h)", text)
        self.assertIn("— close, re-date, or restore evidence", text)
        self.assertTrue(
            text.endswith("Reply on Telegram or run: python manage.py steward_ack <expectation_id> / steward_decide")
        )
        self.assertNotIn("->", text)
        self.assertIn("No expectation", text)
        self.assertNotIn("NEVER_RENDER_PAYLOAD", text)
        self.assertNotIn("NEVER_RENDER_DETAILS", text)
        self.assertNotIn("changes", stats)

    def test_nag_decay_days_and_absorption(self):
        for age in (2, 5, 10, 17, 24):
            with self.subTest(age=age):
                item = self._item(
                    f"shown-{age}",
                    status=TrackedItem.Status.BLOCKED,
                    age_days=age,
                )
                text, _ = render_steward_daily_digest()
                self.assertIn(item.title, text)
                item.delete()

        for age in (3, 4, 6):
            with self.subTest(age=age):
                item = self._item(
                    f"hidden-{age}",
                    status=TrackedItem.Status.BLOCKED,
                    age_days=age,
                )
                text, _ = render_steward_daily_digest()
                self.assertNotIn(item.title, text)
                self.assertIn("1 items waiting (next reminder for oldest in", text)
                item.delete()

    def test_global_length_cap_reports_omitted_lines(self):
        for index in range(30):
            self._item(
                f"blocked-{index}",
                status=TrackedItem.Status.BLOCKED,
                age_days=2,
                context="x" * 1000,
            )
        with patch("apps.steward.digest.MAX_SECTION_LINES", 100):
            text, _ = render_steward_daily_digest()
        self.assertLessEqual(len(text), MAX_DIGEST_CHARS)
        self.assertRegex(text, r"… \+\d+ lines omitted")

    def test_section_budgets_preserve_every_priority_section_under_pressure(self):
        now = timezone.now()
        for index in range(15):
            self._item(
                f"blocked-{index}",
                status=TrackedItem.Status.BLOCKED,
                age_days=2,
                context="n" * 240,
            )
            self._missed(f"missed-{index}")
            self._item(f"orphan-{index}")
            run = EvalRun.objects.create(
                suite=f"unhealthy-{index}",
                trigger=EvalRun.Trigger.SCHEDULED,
                status=EvalRun.Status.FAIL,
                finished_at=now,
            )
            EvalRun.objects.filter(pk=run.pk).update(started_at=now)

        text, _ = render_steward_daily_digest(now=now + timedelta(minutes=1))

        self.assertLessEqual(len(text), MAX_DIGEST_CHARS)
        headings = (
            "NEEDS YOU",
            "STALLED",
            "SLO / EVALS",
            "INTEGRITY",
        )
        for index, heading in enumerate(headings):
            start = text.index(f"{heading} (")
            end = text.index(f"{headings[index + 1]} (") if index + 1 < len(headings) else len(text)
            section = text[start:end]
            self.assertRegex(
                section.splitlines()[0],
                rf"^{re.escape(heading)} \(\d+\)$",
            )
            self.assertGreaterEqual(len(section.splitlines()), 2)
            self.assertTrue(section.splitlines()[1].startswith("- "))
        self.assertRegex(text, r"… \+\d+ lines omitted")

    def test_all_quiet_is_liveness_proof(self):
        now = timezone.now()
        for collector in CollectorStatus.Collector.values:
            CollectorStatus.objects.create(
                collector=collector,
                last_success_at=now,
                last_attempt_at=now,
            )
        text, stats = render_steward_daily_digest(now=now)
        self.assertIn("ALL QUIET", text)
        self.assertIn("All quiet — 0 expectations armed, last sweep unknown.", text)
        self.assertNotIn("Reply on Telegram", text)
        self.assertEqual(sum(stats.values()), 0)

    def test_latest_unhealthy_suite_is_rendered_without_a_transition(self):
        now = timezone.now()
        older = EvalRun.objects.create(
            suite="stuck-suite",
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.FAIL,
            finished_at=now - timedelta(hours=2),
        )
        latest = EvalRun.objects.create(
            suite="stuck-suite",
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.FAIL,
            finished_at=now - timedelta(hours=1),
        )
        healthy = EvalRun.objects.create(
            suite="healthy-suite",
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.PASS,
            finished_at=now,
        )
        EvalRun.objects.filter(pk__in=[older.pk, latest.pk, healthy.pk]).update(started_at=now - timedelta(hours=3))

        text, _ = render_steward_daily_digest(now=now)

        self.assertIn(
            f"EVAL stuck-suite: failing since 1h ago (run {latest.id}) — open the run, fix, or park",
            text,
        )
        self.assertNotIn(f"run {older.id}", text)
        self.assertNotIn("healthy-suite", text)
        self.assertNotIn("ALL QUIET", text)

    def test_latest_failure_older_than_bound_silently_disappears(self):
        now = timezone.now()
        old = EvalRun.objects.create(
            suite="historical-suite",
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.FAIL,
            finished_at=now - timedelta(days=31),
        )
        EvalRun.objects.filter(pk=old.pk).update(
            started_at=now - timedelta(days=31),
        )

        text, _ = render_steward_daily_digest(now=now)

        self.assertIn(
            f"EVAL historical-suite: failing since 31d ago (run {old.id}) — open the run, fix, or park",
            text,
        )
        self.assertNotIn("ALL QUIET", text)

    def test_recent_finish_started_before_bound_silently_disappears(self):
        now = timezone.now()
        run = EvalRun.objects.create(
            suite="long-running-suite",
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.FAIL,
            finished_at=now - timedelta(hours=1),
        )
        EvalRun.objects.filter(pk=run.pk).update(
            started_at=now - timedelta(days=31),
        )

        text, _ = render_steward_daily_digest(now=now)

        self.assertIn(
            f"EVAL long-running-suite: failing since 1h ago (run {run.id}) — open the run, fix, or park",
            text,
        )
        self.assertNotIn("ALL QUIET", text)

    def test_passing_metric_and_nonfailing_evals_are_omitted(self):
        now = timezone.now()
        run = EvalRun.objects.create(
            suite="slo_snapshot",
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.DEGRADED,
            finished_at=now,
        )
        EvalRun.objects.filter(pk=run.pk).update(started_at=now)
        EvalResult.objects.create(
            run=run,
            case_id="reply_latency_p50_ms",
            kind=EvalResult.Kind.SLO,
            passed=True,
            score=None,
            threshold=Decimal("15.000"),
            details={"skipped": True},
        )
        healthy = EvalRun.objects.create(
            suite="healthy-suite",
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.PASS,
            finished_at=now,
        )
        EvalRun.objects.filter(pk=healthy.pk).update(started_at=now)

        text, _ = render_steward_daily_digest(now=now)

        self.assertNotIn("SLO / EVALS", text)
        self.assertNotIn("reply_latency_p50_ms", text)
        self.assertNotIn("slo_snapshot", text)
        self.assertNotIn("healthy-suite", text)

    def test_error_eval_renders_while_degraded_eval_stays_hidden(self):
        now = timezone.now()
        errored = EvalRun.objects.create(
            suite="crashed-suite",
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.ERROR,
            finished_at=now - timedelta(hours=2),
        )
        degraded = EvalRun.objects.create(
            suite="degraded-suite",
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.DEGRADED,
            finished_at=now - timedelta(hours=1),
        )
        EvalRun.objects.filter(pk__in=[errored.pk, degraded.pk]).update(started_at=now - timedelta(hours=3))

        text, _ = render_steward_daily_digest(now=now)

        self.assertIn(
            f"EVAL crashed-suite: errored since 2h ago (run {errored.id}) — open the run, fix, or park",
            text,
        )
        self.assertNotIn("degraded-suite", text)

    def test_changes_never_render_for_trusted_evidence(self):
        now = timezone.now()
        for collector in CollectorStatus.Collector.values:
            CollectorStatus.objects.create(
                collector=collector,
                last_success_at=now,
                last_attempt_at=now,
            )
        EvidenceEvent.objects.create(
            source=EvidenceSource.CI_RUN,
            subject="late-ci",
            occurred_at=now - timedelta(days=3),
            received_at=now,
            payload={},
            fingerprint="late-received",
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )

        text, _ = render_steward_daily_digest(now=now)

        self.assertNotIn("CHANGES (24h)", text)
        self.assertNotIn("ci_run", text)
        self.assertIn("ALL QUIET", text)

    def test_subjects_strip_controls_and_cap_rendered_length(self):
        now = timezone.now()
        dangerous = "safe\u0007\u0085\u202e" + ("x" * 50)
        self._missed(dangerous)
        run = EvalRun.objects.create(
            suite=dangerous,
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.FAIL,
            finished_at=now,
        )
        EvalRun.objects.filter(pk=run.pk).update(started_at=now)

        text, _ = render_steward_daily_digest(now=now + timedelta(minutes=1))

        for control in ("\u0007", "\u0085", "\u202e"):
            self.assertNotIn(control, text)
        transition_line = next(line for line in text.splitlines() if line.startswith("- EVAL safe"))
        rendered_suite = transition_line.removeprefix("- EVAL ").split(":", 1)[0]
        self.assertLessEqual(len(rendered_suite), 64)

    @patch(
        "apps.steward.digest.send_digest",
        side_effect=RuntimeError("delivery down"),
    )
    @patch(
        "apps.steward.digest.collect_eval_evidence",
        return_value={"created": 0},
    )
    def test_delivery_failure_still_writes_digest_record(self, _collect, _send):
        result = run_steward_daily_digest()
        record = DigestRecord.objects.get()
        self.assertEqual(record.delivery, DigestRecord.Delivery.TRANSIENT)
        self.assertEqual(record.body, "STEWARD DAILY FACTS\n" + record.body.split("\n", 1)[1])
        self.assertEqual(result["digest_id"], record.id)

    @patch("apps.steward.digest.send_digest", return_value="delivered")
    @patch(
        "apps.steward.digest.collect_eval_evidence",
        return_value={"created": 0},
    )
    def test_same_utc_date_retry_skips_second_send(self, _collect, send):
        now = timezone.now()
        with patch("apps.steward.digest.timezone.now", return_value=now):
            first = run_steward_daily_digest()
            retry = run_steward_daily_digest()

        self.assertFalse(first["skipped"])
        self.assertTrue(retry["skipped"])
        self.assertEqual(first["digest_id"], retry["digest_id"])
        send.assert_called_once()
        self.assertEqual(DigestRecord.objects.count(), 1)

    @patch("apps.steward.digest.send_digest", return_value="delivered")
    @patch(
        "apps.steward.digest.collect_eval_evidence",
        side_effect=[RuntimeError("collector unavailable"), {"created": 0}],
    )
    def test_collection_failure_does_not_claim_date_and_same_day_retry_sends(
        self,
        collect,
        send,
    ):
        now = timezone.now()
        with patch("apps.steward.digest.timezone.now", return_value=now):
            with self.assertRaisesRegex(RuntimeError, "collector unavailable"):
                run_steward_daily_digest()
            self.assertFalse(DigestRecord.objects.exists())

            retry = run_steward_daily_digest()

        self.assertFalse(retry["skipped"])
        self.assertEqual(collect.call_count, 2)
        send.assert_called_once()
        self.assertEqual(
            DigestRecord.objects.get().delivery,
            DigestRecord.Delivery.DELIVERED,
        )

    @patch(
        "apps.steward.digest.collect_eval_evidence",
        return_value={"created": 0},
    )
    def test_hard_exit_after_claim_burns_day(self, _collect):
        now = timezone.now()
        with (
            patch("apps.steward.digest.timezone.now", return_value=now),
            patch(
                "apps.steward.digest.send_digest",
                side_effect=[SystemExit("worker killed"), DigestRecord.Delivery.DELIVERED],
            ) as send,
        ):
            with self.assertRaisesRegex(SystemExit, "worker killed"):
                run_steward_daily_digest()

            burned = DigestRecord.objects.get()
            self.assertEqual(burned.delivery, DigestRecord.Delivery.TRANSIENT)
            self.assertEqual(burned.body, "")

            retry = run_steward_daily_digest()

        self.assertFalse(retry["skipped"])
        self.assertEqual(send.call_count, 2)
        burned.refresh_from_db()
        self.assertEqual(burned.delivery, DigestRecord.Delivery.DELIVERED)
        self.assertTrue(burned.body)

    def test_integrity_flags_are_soft_and_linked_armed_expectation_clears_flag(self):
        active = self._item("Active orphan")
        parked = self._item("Parked orphan", status=TrackedItem.Status.PARKED)
        linked = self._item("Active linked")
        Expectation.objects.create(
            kind=Expectation.Kind.DEADLINE,
            due_at=timezone.now() + timedelta(days=7),
            grace_s=3600,
            evidence_source=EvidenceSource.MJ_ACK,
            subject="linked",
            state=Expectation.State.ARMED,
            on_miss=Expectation.OnMiss.DIGEST,
            subject_item=linked,
        )

        text, _ = render_steward_daily_digest()

        self.assertIn(active.title, text)
        self.assertIn("active with zero armed expectations", text)
        self.assertIn(parked.title, text)
        self.assertIn("parked with no revisit expectation", text)
        self.assertNotIn(linked.title, text)
