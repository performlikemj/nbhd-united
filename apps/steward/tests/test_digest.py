from __future__ import annotations

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

    def test_renders_every_facts_section_and_no_payload_or_details(self):
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
                "status": EvalRun.Status.PASS,
                "prev_status": EvalRun.Status.FAIL,
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
            "CHANGES (24h)",
            "INTEGRITY",
        ):
            self.assertIn(heading, text)
        self.assertIn("Needs decision", text)
        self.assertIn("deadline-one", text)
        self.assertIn("reply_latency_p50_ms", text)
        self.assertIn("journey: fail -> pass", text)
        self.assertIn("No expectation", text)
        self.assertNotIn("NEVER_RENDER_PAYLOAD", text)
        self.assertNotIn("NEVER_RENDER_DETAILS", text)
        self.assertGreater(stats["changes"], 0)

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
        self.assertRegex(text, r"… \+\d+ lines omitted$")

    def test_all_quiet_is_liveness_proof(self):
        text, stats = render_steward_daily_digest()
        self.assertIn("ALL QUIET", text)
        self.assertIn("All quiet — 0 expectations armed, last sweep unknown.", text)
        self.assertEqual(sum(stats.values()), 0)

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
