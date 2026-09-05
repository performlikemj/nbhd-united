from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.evals.models import EvalResult, EvalRun
from apps.steward.collectors.evals import collect_eval_evidence
from apps.steward.digest import render_steward_daily_digest
from apps.steward.models import EvidenceEvent, EvidenceSource


@override_settings(STEWARD_INGEST_SECRET="obvious-test-steward-secret")
class EvalEvidenceCollectorTests(TestCase):
    def _post_evidence(self, body):
        raw = json.dumps(body, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            b"obvious-test-steward-secret",
            timestamp.encode() + b"." + raw,
            hashlib.sha256,
        ).hexdigest()
        return self.client.post(
            "/api/steward/evidence/",
            data=raw,
            content_type="application/json",
            headers={
                "X-Steward-Timestamp": timestamp,
                "X-Steward-Signature": signature,
            },
        )

    def _run(
        self,
        suite: str,
        status: str,
        when,
        *,
        passed=True,
        run_id=None,
    ) -> EvalRun:
        run = EvalRun.objects.create(
            id=run_id,
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
            f"eval_run:eval-run:journey:{failing.id}",
        )
        self.assertEqual(
            event.payload,
            {
                "run_id": failing.id,
                "status": EvalRun.Status.FAIL,
                "prev_status_at_collection": EvalRun.Status.PASS,
                "passed": 0,
                "total": 1,
                "git_sha": "abc123",
            },
        )
        self.assertNotIn("NEVER_COPY_DETAILS", str(event.payload))

    def test_no_status_change_produces_no_event(self):
        now = timezone.now()
        self._run("journey", EvalRun.Status.PASS, now - timedelta(hours=2))
        self._run("journey", EvalRun.Status.PASS, now - timedelta(hours=1))

        collect_eval_evidence()

        self.assertFalse(EvidenceEvent.objects.filter(source=EvidenceSource.EVAL_RUN).exists())

    def test_late_terminal_replay_emits_only_per_run_facts_without_edges(self):
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
            EvalRun.Status.FAIL,
            now - timedelta(hours=1),
            passed=False,
        )

        first = collect_eval_evidence()
        self.assertEqual(first["eval_run"], 1)

        EvalRun.objects.filter(pk=late.pk).update(
            status=EvalRun.Status.DEGRADED,
            finished_at=now - timedelta(hours=2),
        )
        second = collect_eval_evidence()

        self.assertEqual(second["eval_run"], 1)
        self.assertSetEqual(
            set(EvidenceEvent.objects.filter(source=EvidenceSource.EVAL_RUN).values_list("fingerprint", flat=True)),
            {
                f"eval_run:eval-run:journey:{late.id}",
                f"eval_run:eval-run:journey:{newer.id}",
            },
        )
        self.assertEqual(
            EvidenceEvent.objects.get(
                fingerprint=f"eval_run:eval-run:journey:{newer.id}",
            ).payload["prev_status_at_collection"],
            EvalRun.Status.PASS,
        )
        text, _ = render_steward_daily_digest(now=now)
        self.assertNotIn(f"run {late.id}", text)
        self.assertIn(
            f"EVAL journey: failing since 1h ago (run {newer.id}) — open the run, fix, or park",
            text,
        )
        self.assertNotIn("->", text)

    def test_external_fingerprint_squat_cannot_suppress_internal_run_event(self):
        now = timezone.now()
        self._run("journey", EvalRun.Status.PASS, now - timedelta(hours=2))
        run = self._run(
            "journey",
            EvalRun.Status.FAIL,
            now - timedelta(hours=1),
            passed=False,
            run_id=999,
        )

        response = self._post_evidence(
            {
                "source": "ci_run",
                "subject": "attacker-controlled-ci",
                "fingerprint": "eval-transition:journey:999",
            },
        )
        collected = collect_eval_evidence()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(collected["eval_run"], 1)
        self.assertSetEqual(
            set(EvidenceEvent.objects.values_list("fingerprint", flat=True)),
            {
                "ci_run:eval-transition:journey:999",
                "eval_run:eval-run:journey:999",
            },
        )

    def test_external_current_run_fingerprint_cannot_collide_across_source(self):
        now = timezone.now()
        self._run("journey", EvalRun.Status.PASS, now - timedelta(hours=2))
        self._run(
            "journey",
            EvalRun.Status.FAIL,
            now - timedelta(hours=1),
            passed=False,
            run_id=1000,
        )

        response = self._post_evidence(
            {
                "source": "ci_run",
                "subject": "attacker-controlled-ci",
                "fingerprint": "eval-run:journey:1000",
            },
        )
        collected = collect_eval_evidence()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(collected["eval_run"], 1)
        self.assertSetEqual(
            set(EvidenceEvent.objects.values_list("fingerprint", flat=True)),
            {
                "ci_run:eval-run:journey:1000",
                "eval_run:eval-run:journey:1000",
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

    def test_twenty_run_replay_stays_within_query_budget(self):
        now = timezone.now()
        for index in range(20):
            passed = index % 2 == 0
            status = EvalRun.Status.PASS if passed else EvalRun.Status.FAIL
            run = EvalRun.objects.create(
                suite="slo_snapshot",
                trigger=EvalRun.Trigger.SCHEDULED,
                status=status,
                finished_at=now - timedelta(hours=20 - index),
                git_sha=f"sha-{index}",
            )
            EvalRun.objects.filter(pk=run.pk).update(
                started_at=now - timedelta(hours=20 - index, minutes=1),
            )
            EvalResult.objects.create(
                run=run,
                case_id="reply_latency_p50_ms",
                kind=EvalResult.Kind.SLO,
                passed=passed,
                score=Decimal("10.000") if passed else Decimal("40.000"),
                threshold=Decimal("15.000"),
                details={"skipped": False},
            )

        with CaptureQueriesContext(connection) as queries:
            collected = collect_eval_evidence()

        self.assertEqual(collected["eval_run"], 19)
        self.assertEqual(collected["eval_slo"], 19)
        self.assertLessEqual(
            len(queries),
            15,
            "\n".join(query["sql"] for query in queries),
        )
