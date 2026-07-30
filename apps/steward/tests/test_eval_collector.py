from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.evals.models import EvalResult, EvalRun
from apps.steward.collectors.evals import collect_eval_evidence
from apps.steward.models import EvidenceEvent, EvidenceSource


class EvalEvidenceCollectorTests(TestCase):
    def _run(self, suite: str, status: str, when, *, passed=True) -> EvalRun:
        run = EvalRun.objects.create(
            suite=suite,
            trigger=EvalRun.Trigger.SCHEDULED,
            status=status,
            finished_at=when,
            git_sha="abc123",
        )
        EvalRun.objects.filter(pk=run.pk).update(started_at=when - timedelta(minutes=1))
        run.refresh_from_db()
        EvalResult.objects.create(
            run=run,
            case_id="case",
            kind=EvalResult.Kind.JOURNEY,
            passed=passed,
            details={"poison": "NEVER_COPY_DETAILS"},
        )
        return run

    def test_suite_transition_is_exactly_once_and_metadata_only(self):
        now = timezone.now()
        self._run("journey", EvalRun.Status.PASS, now - timedelta(hours=2))
        failing = self._run(
            "journey",
            EvalRun.Status.FAIL,
            now - timedelta(hours=1),
            passed=False,
        )

        first = collect_eval_evidence()
        second = collect_eval_evidence()

        self.assertEqual(first["eval_run"], 1)
        self.assertEqual(second["created"], 0)
        event = EvidenceEvent.objects.get(source=EvidenceSource.EVAL_RUN)
        self.assertEqual(
            event.fingerprint,
            f"eval-transition:journey:{failing.id}",
        )
        self.assertEqual(event.payload["status"], EvalRun.Status.FAIL)
        self.assertEqual(event.payload["prev_status"], EvalRun.Status.PASS)
        self.assertNotIn("NEVER_COPY_DETAILS", str(event.payload))

    def test_no_status_change_produces_no_event(self):
        now = timezone.now()
        self._run("journey", EvalRun.Status.PASS, now - timedelta(hours=2))
        self._run("journey", EvalRun.Status.PASS, now - timedelta(hours=1))

        collect_eval_evidence()

        self.assertFalse(EvidenceEvent.objects.filter(source=EvidenceSource.EVAL_RUN).exists())

    def test_late_terminal_commit_is_replayed_after_newer_run_was_collected(self):
        now = timezone.now()
        self._run("journey", EvalRun.Status.PASS, now - timedelta(hours=3))
        late = EvalRun.objects.create(
            suite="journey",
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.RUNNING,
            git_sha="late",
        )
        EvalResult.objects.create(
            run=late,
            case_id="late-case",
            kind=EvalResult.Kind.JOURNEY,
            passed=False,
            details={},
        )
        newer = self._run(
            "journey",
            EvalRun.Status.PASS,
            now - timedelta(hours=1),
        )

        first = collect_eval_evidence()
        self.assertEqual(first["eval_run"], 0)

        EvalRun.objects.filter(pk=late.pk).update(
            status=EvalRun.Status.FAIL,
            finished_at=now - timedelta(hours=2),
        )
        second = collect_eval_evidence()

        self.assertEqual(second["eval_run"], 2)
        self.assertSetEqual(
            set(EvidenceEvent.objects.filter(source=EvidenceSource.EVAL_RUN).values_list("fingerprint", flat=True)),
            {
                f"eval-transition:journey:{late.id}",
                f"eval-transition:journey:{newer.id}",
            },
        )

    def test_slo_transition_serializes_decimals_and_never_details(self):
        now = timezone.now()
        previous = self._run(
            "slo_snapshot",
            EvalRun.Status.PASS,
            now - timedelta(days=1),
        )
        current = self._run(
            "slo_snapshot",
            EvalRun.Status.FAIL,
            now,
            passed=False,
        )
        previous.results.all().delete()
        current.results.all().delete()
        EvalResult.objects.create(
            run=previous,
            case_id="reply_latency_p50_ms",
            kind=EvalResult.Kind.SLO,
            passed=True,
            score=Decimal("10.125"),
            threshold=Decimal("15.000"),
            details={"skipped": False, "poison": "NEVER_COPY_SLO_DETAILS"},
        )
        EvalResult.objects.create(
            run=current,
            case_id="reply_latency_p50_ms",
            kind=EvalResult.Kind.SLO,
            passed=False,
            score=Decimal("40.250"),
            threshold=Decimal("15.000"),
            details={"skipped": False, "poison": "NEVER_COPY_SLO_DETAILS"},
        )

        collect_eval_evidence()
        collect_eval_evidence()

        event = EvidenceEvent.objects.get(source=EvidenceSource.EVAL_SLO)
        self.assertEqual(event.payload["score"], "40.250")
        self.assertEqual(event.payload["threshold"], "15.000")
        self.assertEqual(event.payload["breach_days"], 1)
        self.assertNotIn("NEVER_COPY_SLO_DETAILS", str(event.payload))
        self.assertEqual(
            EvidenceEvent.objects.filter(source=EvidenceSource.EVAL_SLO).count(),
            1,
        )
