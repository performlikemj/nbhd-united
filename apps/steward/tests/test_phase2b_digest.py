from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.steward.digest import MAX_DIGEST_CHARS, render_steward_daily_digest
from apps.steward.models import (
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

    def test_trains_then_stalled_and_repos_after_slo_evals(self):
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

        with (
            patch("httpx.Client") as http_client,
            patch("requests.get") as requests_get,
        ):
            text, stats = render_steward_daily_digest(now=now)

        http_client.assert_not_called()
        requests_get.assert_not_called()
        self.assertLess(text.index("TRAINS ("), text.index("STALLED ("))
        self.assertLess(text.index("SLO / EVALS ("), text.index("REPOS ("))
        self.assertIn(
            "nbhd_ios 2.1.6: planned (2d) — next: integrating due",
            text,
        )
        self.assertIn(
            "nbhd-ios: 2 open PRs (2 stale>7d, 1 drafts>7d), dependabot: 1, main CI: failure",
            text,
        )
        self.assertIn("#7 Stalehidden title — 9d quiet", text)
        self.assertNotIn("Dependency bump must not be named", text)
        self.assertNotIn("\u202e", text)
        self.assertNotIn("\x1f", text)
        self.assertEqual(stats["trains"], 1)
        self.assertEqual(stats["repos"], 1)

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
