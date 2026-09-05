from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.evals.models import EvalRun
from apps.steward.digest import MAX_DIGEST_CHARS, render_steward_daily_digest
from apps.steward.models import (
    CollectorStatus,
    EvidenceEvent,
    EvidenceSource,
    Expectation,
    ReleaseTrain,
    RepoPullRequest,
    TrackedItem,
)
from apps.steward.trains import open_train


class Phase2bDigestTests(TestCase):
    def _pull_request(
        self,
        *,
        repo: str,
        number: int,
        title: str,
        age_days: int,
        draft: bool = False,
        dependabot: bool = False,
        now=None,
    ):
        now = now or timezone.now()
        return RepoPullRequest.objects.create(
            repo=repo,
            number=number,
            title=title,
            author="dependabot[bot]" if dependabot else "mj",
            draft=draft,
            state=RepoPullRequest.State.OPEN,
            opened_at=now - timedelta(days=age_days + 2),
            last_activity_at=now - timedelta(days=age_days),
            is_dependabot=dependabot,
            head_ref=f"feature-{number}",
            synced_at=now,
        )

    def test_stalled_then_trains_and_actionable_human_repos(self):
        now = timezone.now()
        train = open_train(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.6",
        )
        ReleaseTrain.objects.filter(pk=train.pk).update(phase_changed_at=now - timedelta(days=2))
        self._pull_request(
            repo="nbhd-ios",
            number=7,
            title="Stale\u202ehidden\x1f title",
            age_days=9,
            draft=True,
            now=now,
        )
        self._pull_request(
            repo="nbhd-ios",
            number=8,
            title="Dependency bump must not be named",
            age_days=10,
            dependabot=True,
            now=now,
        )
        self._pull_request(
            repo="nbhd-ios",
            number=9,
            title="Fresh human PR must not be named",
            age_days=3,
            now=now,
        )
        EvidenceEvent.objects.create(
            source=EvidenceSource.CI_RUN,
            subject="nbhd-ios-main-ci",
            occurred_at=now,
            payload={"conclusion": "failure"},
            fingerprint="digest-ci-failure",
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )
        EvidenceEvent.objects.create(
            source=EvidenceSource.EVAL_RUN,
            subject="eval:phase2b",
            occurred_at=now,
            payload={
                "run_id": 1,
                "status": "fail",
                "prev_status_at_collection": "pass",
                "passed": 0,
                "total": 1,
                "git_sha": "abc123",
            },
            fingerprint="digest-eval-failure",
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )
        Expectation.objects.create(
            kind=Expectation.Kind.DEADLINE,
            due_at=now - timedelta(days=1),
            grace_s=3600,
            evidence_source=EvidenceSource.MJ_ACK,
            subject="phase2b-stalled",
            state=Expectation.State.MISSED,
            on_miss=Expectation.OnMiss.DIGEST,
        )
        eval_run = EvalRun.objects.create(
            suite="phase2b",
            trigger=EvalRun.Trigger.SCHEDULED,
            status=EvalRun.Status.FAIL,
            finished_at=now,
        )
        EvalRun.objects.filter(pk=eval_run.pk).update(started_at=now)

        with (
            patch("httpx.Client") as http_client,
            patch("requests.get") as requests_get,
        ):
            text, stats = render_steward_daily_digest(now=now)

        http_client.assert_not_called()
        requests_get.assert_not_called()
        self.assertLess(text.index("STALLED ("), text.index("TRAINS ("))
        self.assertLess(text.index("SLO / EVALS ("), text.index("REPOS ("))
        self.assertIn(
            "nbhd_ios 2.1.6: planned (2d) — next: integrating due",
            text,
        )
        self.assertIn(
            "- nbhd-ios #7 — Stalehidden title — 9d quiet — review, rebase, or close",
            text,
        )
        self.assertNotIn("Dependency bump must not be named", text)
        self.assertNotIn("Fresh human PR must not be named", text)
        self.assertNotIn("open PRs", text)
        self.assertNotIn("dependabot:", text)
        self.assertNotIn("main CI", text)
        self.assertIn("— close, re-date, or restore evidence", text)
        self.assertNotIn("\u202e", text)
        self.assertNotIn("\x1f", text)
        self.assertEqual(stats["trains"], 1)
        self.assertEqual(stats["repos"], 1)

    def test_openrouter_only_renders_severe_actionable_findings(self):
        now = timezone.now()
        common = {
            "date": now.date().isoformat(),
            "scope": "account",
            "model": "provider/model",
            "baseline_days": 3,
        }
        for suffix, payload in (
            (
                "nonsevere-null",
                {
                    **common,
                    "kind": "null_rate",
                    "current_pct": 1.0,
                    "severe": False,
                },
            ),
            (
                "severe-tool-share",
                {
                    **common,
                    "kind": "tool_calls_share_drop",
                    "current_pct": 10.0,
                    "baseline_pct": 50.0,
                    "drop_pts": 40.0,
                    "severe": True,
                },
            ),
            (
                "severe-null",
                {
                    **common,
                    "scope": "canary",
                    "model": "fallback/model",
                    "kind": "null_rate",
                    "current_pct": 3.25,
                    "severe": True,
                },
            ),
            (
                "new-provider",
                {
                    "kind": "new_provider",
                    "date": now.date().isoformat(),
                    "scope": "provider",
                    "provider": "new-provider",
                    "baseline_days": 3,
                    "severe": False,
                },
            ),
        ):
            EvidenceEvent.objects.create(
                source=EvidenceSource.OPENROUTER_MODEL_HEALTH,
                subject=f"openrouter-health:{suffix}",
                occurred_at=now,
                received_at=now,
                payload=payload,
                fingerprint=f"openrouter-health:{suffix}",
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )

        text, stats = render_steward_daily_digest(now=now)

        self.assertIn(
            "- canary fallback/model: null finish_reason 3.25% (> 0.50%) "
            "— switch model/provider route or set a fallback",
            text,
        )
        self.assertIn(
            "- account provider/model: tool_calls 10.00% vs 50.00% (40.00 pts drop) "
            "— model stopped using tools; check the model/provider change",
            text,
        )
        self.assertNotIn("1.00%", text)
        self.assertNotIn("new-provider", text)
        self.assertEqual(stats["openrouter"], 2)

    def test_terminal_train_is_shown_only_on_close_day(self):
        now = timezone.now()
        ReleaseTrain.objects.create(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.5",
            phase=ReleaseTrain.Phase.RELEASED,
            phase_changed_at=now,
        )

        close_day, _ = render_steward_daily_digest(now=now)
        next_day, _ = render_steward_daily_digest(now=now + timedelta(days=1))

        self.assertIn("nbhd_ios 2.1.5: released (0d)", close_day)
        self.assertNotIn("nbhd_ios 2.1.5", next_day)

    def test_train_and_repo_sections_obey_global_budget(self):
        now = timezone.now()
        for index in range(30):
            open_train(
                product=TrackedItem.Product.NBHD_IOS,
                version_string=f"2.2.{index}",
            )
            self._pull_request(
                repo=f"repo-{index:02d}",
                number=index + 1,
                title="Very stale deterministic title " + ("x" * 100),
                age_days=20,
            )

        text, _ = render_steward_daily_digest(now=now)

        self.assertLessEqual(len(text), MAX_DIGEST_CHARS)
        self.assertIn("TRAINS (30)", text)
        self.assertIn("REPOS (30)", text)
        self.assertIn("lines omitted", text)

    def test_budget_floor_preserves_stall_identity_after_long_needs_you_and_trains(self):
        now = timezone.now()
        for index in range(10):
            TrackedItem.objects.create(
                product=TrackedItem.Product.PORTFOLIO,
                kind=TrackedItem.Kind.BLOCKED_ON_MJ,
                title=f"needs-{index}-" + ("x" * 180),
                context="c" * 240,
                status=TrackedItem.Status.BLOCKED,
                provenance=EvidenceEvent.Provenance.MJ,
                status_changed_at=now - timedelta(days=2),
            )
        for index in range(30):
            open_train(
                product=TrackedItem.Product.NBHD_IOS,
                version_string=f"3.0.{index}",
            )
        Expectation.objects.create(
            kind=Expectation.Kind.DEADLINE,
            due_at=now - timedelta(days=1),
            grace_s=3600,
            evidence_source=EvidenceSource.MJ_ACK,
            subject="adversarial-stall-subject",
            state=Expectation.State.MISSED,
            on_miss=Expectation.OnMiss.DIGEST,
        )

        text, _ = render_steward_daily_digest(now=now)

        self.assertLessEqual(len(text), MAX_DIGEST_CHARS)
        self.assertLess(text.index("NEEDS YOU ("), text.index("STALLED ("))
        self.assertLess(text.index("STALLED ("), text.index("TRAINS ("))
        self.assertIn("adversarial-stall-subject", text)

    def test_integrity_renders_missing_not_configured_and_stale_collectors_without_repos(self):
        now = timezone.now()
        text, _ = render_steward_daily_digest(now=now)
        self.assertIn("collector github: never succeeded", text)
        self.assertIn("collector asc: never succeeded", text)
        self.assertNotIn("REPOS (", text)

        CollectorStatus.objects.create(
            collector=CollectorStatus.Collector.GITHUB,
            last_attempt_at=now,
            last_error_class="not_configured",
            consecutive_failures=1,
        )
        CollectorStatus.objects.create(
            collector=CollectorStatus.Collector.ASC,
            last_attempt_at=now,
            last_success_at=now - timedelta(hours=4),
        )

        text, _ = render_steward_daily_digest(now=now)

        self.assertIn("collector github: not_configured", text)
        self.assertIn("collector asc: stale", text)
        self.assertNotIn("REPOS (", text)

    def test_collector_is_stale_at_exactly_three_intervals(self):
        now = timezone.now()
        CollectorStatus.objects.create(
            collector=CollectorStatus.Collector.GITHUB,
            last_attempt_at=now,
            last_success_at=now - timedelta(minutes=90),
        )

        text, _ = render_steward_daily_digest(now=now)

        self.assertIn("collector github: stale", text)
