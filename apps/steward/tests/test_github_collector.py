from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.steward.collectors import github
from apps.steward.models import (
    CollectorStatus,
    EvidenceEvent,
    EvidenceSource,
    GithubTagSnapshot,
    ReleaseTrain,
    RepoPullRequest,
    TrackedItem,
)
from apps.steward.trains import advance_train, open_train

TEST_REPOS = [("owner", "nbhd-united", TrackedItem.Product.NBHD_UNITED)]
TS = "2026-07-30T12:00:00Z"
TS2 = "2026-07-30T13:00:00Z"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def pull_request(
    number: int,
    *,
    state: str = "open",
    merged_at: str | None = None,
    author: str = "mj",
    draft: bool = False,
    title: str = "A pull request",
    updated_at: str = TS,
):
    return {
        "number": number,
        "title": title,
        "user": {"login": author},
        "draft": draft,
        "state": state,
        "created_at": TS,
        "updated_at": updated_at,
        "merged_at": merged_at,
        "head": {"ref": f"feature-{number}"},
    }


def workflow_run(
    run_id: int,
    *,
    conclusion: str = "success",
    run_attempt: int | None = 1,
    head_sha: str = "a" * 40,
    updated_at: str = TS,
    name: str = "CI",
):
    payload = {
        "id": run_id,
        "status": "completed",
        "conclusion": conclusion,
        "head_branch": "main",
        "head_sha": head_sha,
        "name": name,
        "updated_at": updated_at,
    }
    if run_attempt is not None:
        payload["run_attempt"] = run_attempt
    return payload


class GitHubCollectorTests(TestCase):
    def _client(
        self,
        *,
        open_prs=None,
        all_prs=None,
        all_pages=None,
        runs=None,
        tags=None,
        individual=None,
    ):
        open_prs = open_prs or []
        all_prs = all_prs if all_prs is not None else open_prs
        all_pages = all_pages or {}
        runs = runs or []
        tags = tags or []
        individual = individual or {}
        client = MagicMock()

        def get(path, params=None, timeout=None):
            params = params or {}
            if path == "/repos/owner/nbhd-united":
                return FakeResponse({"default_branch": "main"})
            if path.startswith("/repos/owner/nbhd-united/pulls/"):
                number = int(path.rsplit("/", 1)[1])
                return FakeResponse(individual[number])
            if path.endswith("/pulls"):
                if params["state"] == "open":
                    return FakeResponse(open_prs)
                return FakeResponse(all_pages.get(params["page"], all_prs))
            if path.endswith("/actions/runs"):
                return FakeResponse({"workflow_runs": runs})
            if path.endswith("/tags"):
                return FakeResponse(tags)
            raise AssertionError(f"unexpected GitHub path {path}")

        client.get.side_effect = get
        context = MagicMock()
        context.__enter__.return_value = client
        context.__exit__.return_value = False
        return context, client

    @override_settings(STEWARD_GITHUB_TOKEN="")
    @patch("apps.steward.collectors.github.httpx.Client")
    def test_disabled_without_token_records_not_configured(self, client_class):
        self.assertEqual(github.collect_github()["repos"], 0)
        client_class.assert_not_called()
        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.GITHUB)
        self.assertEqual(status.last_error_class, "not_configured")
        self.assertIsNone(status.last_success_at)
        self.assertEqual(status.consecutive_failures, 1)

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_recent_merge_on_second_page_emits_without_title(self):
        first_context, _ = self._client(open_prs=[pull_request(1)])
        with patch("apps.steward.collectors.github.httpx.Client", return_value=first_context):
            github.collect_github()

        page_one = [pull_request(number, updated_at=TS2) for number in range(1000, 1100)]
        merged = pull_request(
            1,
            state="closed",
            merged_at=TS2,
            title="SECRET PR TITLE",
            updated_at=TS2,
        )
        second_context, client = self._client(
            open_prs=[],
            all_pages={
                1: page_one,
                2: [merged, pull_request(999, updated_at=TS)],
            },
            individual={1: merged},
        )
        with patch("apps.steward.collectors.github.httpx.Client", return_value=second_context):
            result = github.collect_github()

        self.assertEqual(result["evidence"], 1)
        self.assertEqual(RepoPullRequest.objects.get(number=1).state, RepoPullRequest.State.MERGED)
        event = EvidenceEvent.objects.get(source=EvidenceSource.GITHUB_STATE)
        self.assertEqual(event.payload, {"number": 1, "merged_at": TS2})
        self.assertNotIn("SECRET PR TITLE", str(event.payload))
        all_pages = [
            call.kwargs["params"]["page"]
            for call in client.get.call_args_list
            if call.args[0].endswith("/pulls") and call.kwargs["params"]["state"] == "all"
        ]
        self.assertEqual(all_pages, [1, 2])

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_absent_mirrored_open_pr_is_individually_reconciled(self):
        RepoPullRequest.objects.create(
            repo="nbhd-united",
            number=9,
            title="DB-only title",
            author="mj",
            draft=False,
            state=RepoPullRequest.State.OPEN,
            opened_at=datetime(2026, 7, 20, tzinfo=UTC),
            last_activity_at=datetime(2026, 7, 21, tzinfo=UTC),
            is_dependabot=False,
            head_ref="feature-9",
            synced_at=datetime(2026, 7, 21, tzinfo=UTC),
        )
        closed = pull_request(9, state="closed", updated_at=TS2)
        context, client = self._client(open_prs=[], all_prs=[], individual={9: closed})

        with patch("apps.steward.collectors.github.httpx.Client", return_value=context):
            github.collect_github()

        self.assertEqual(RepoPullRequest.objects.get(number=9).state, RepoPullRequest.State.CLOSED)
        self.assertTrue(any(call.args[0] == "/repos/owner/nbhd-united/pulls/9" for call in client.get.call_args_list))

    def _pushed_train(self, version: str, *, sha: str | None, changed_at: datetime) -> ReleaseTrain:
        train = open_train(
            product=TrackedItem.Product.NBHD_UNITED,
            version_string=version,
        )
        train = advance_train(
            train,
            ReleaseTrain.Phase.PUSHED,
            provenance=EvidenceEvent.Provenance.MJ,
        )
        ReleaseTrain.objects.filter(pk=train.pk).update(
            head_sha=sha,
            phase_changed_at=changed_at,
        )
        train.refresh_from_db()
        return train

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_ci_auto_advance_requires_train_sha_match_and_fresh_event(self):
        occurred_at = datetime.fromisoformat(TS.replace("Z", "+00:00"))
        matching = self._pushed_train(
            "matching",
            sha="a" * 40,
            changed_at=occurred_at - timedelta(minutes=1),
        )
        no_sha = self._pushed_train(
            "no-sha",
            sha=None,
            changed_at=occurred_at - timedelta(minutes=1),
        )
        wrong_sha = self._pushed_train(
            "wrong-sha",
            sha="b" * 40,
            changed_at=occurred_at - timedelta(minutes=1),
        )
        stale = self._pushed_train(
            "stale",
            sha="a" * 40,
            changed_at=occurred_at + timedelta(minutes=1),
        )
        context, _ = self._client(runs=[workflow_run(55)])

        with patch("apps.steward.collectors.github.httpx.Client", return_value=context):
            result = github.collect_github()

        matching.refresh_from_db()
        no_sha.refresh_from_db()
        wrong_sha.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(matching.phase, ReleaseTrain.Phase.CI_GREEN)
        self.assertEqual(no_sha.phase, ReleaseTrain.Phase.PUSHED)
        self.assertEqual(wrong_sha.phase, ReleaseTrain.Phase.PUSHED)
        self.assertEqual(stale.phase, ReleaseTrain.Phase.PUSHED)
        self.assertEqual(result["train_advances"], 1)

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_ci_workflow_binding_rejects_docs_run_and_unbound_ambiguity_is_reported(self):
        occurred_at = datetime.fromisoformat(TS.replace("Z", "+00:00"))
        bound = self._pushed_train(
            "bound",
            sha="a" * 40,
            changed_at=occurred_at - timedelta(minutes=1),
        )
        ReleaseTrain.objects.filter(pk=bound.pk).update(ci_workflow="CI")

        docs_context, _ = self._client(runs=[workflow_run(60, name="Docs")])
        with patch(
            "apps.steward.collectors.github.httpx.Client",
            return_value=docs_context,
        ):
            github.collect_github()
        bound.refresh_from_db()
        self.assertEqual(bound.phase, ReleaseTrain.Phase.PUSHED)

        unbound = self._pushed_train(
            "ambiguous",
            sha="b" * 40,
            changed_at=occurred_at - timedelta(minutes=1),
        )
        ambiguous_context, _ = self._client(
            runs=[
                workflow_run(61, head_sha="b" * 40, name="CI"),
                workflow_run(62, head_sha="b" * 40, name="Docs"),
            ]
        )
        with patch(
            "apps.steward.collectors.github.httpx.Client",
            return_value=ambiguous_context,
        ):
            result = github.collect_github()

        unbound.refresh_from_db()
        self.assertEqual(unbound.phase, ReleaseTrain.Phase.PUSHED)
        self.assertEqual(result["train_advances"], 0)
        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.GITHUB)
        self.assertIn("nbhd-united:ci_workflow_ambiguous=2", status.detail)

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_ci_recovery_sweep_advances_from_previously_persisted_evidence(self):
        occurred_at = datetime.fromisoformat(TS.replace("Z", "+00:00"))
        train = self._pushed_train(
            "recover",
            sha="a" * 40,
            changed_at=occurred_at - timedelta(minutes=1),
        )
        ReleaseTrain.objects.filter(pk=train.pk).update(ci_workflow="CI")
        EvidenceEvent.objects.create(
            source=EvidenceSource.CI_RUN,
            subject="nbhd-united-main-ci",
            occurred_at=occurred_at,
            payload={
                "conclusion": "success",
                "head_sha": "a" * 40,
                "workflow": "CI",
                "run_attempt": 1,
            },
            fingerprint="ci-recovery-probe",
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )
        context, _ = self._client(runs=[])

        with patch(
            "apps.steward.collectors.github.httpx.Client",
            return_value=context,
        ):
            result = github.collect_github()

        train.refresh_from_db()
        self.assertEqual(train.phase, ReleaseTrain.Phase.CI_GREEN)
        self.assertEqual(result["evidence"], 0)
        self.assertEqual(result["train_advances"], 1)

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_ci_rebind_rejects_success_that_predates_binding_epoch(self):
        phase_changed_at = datetime(2026, 7, 30, 10, tzinfo=UTC)
        stale_success_at = datetime(2026, 7, 30, 11, tzinfo=UTC)
        rebound_at = datetime(2026, 7, 30, 12, tzinfo=UTC)
        train = self._pushed_train(
            "rebound",
            sha="a" * 40,
            changed_at=phase_changed_at,
        )
        ReleaseTrain.objects.filter(pk=train.pk).update(ci_workflow="CI")
        EvidenceEvent.objects.create(
            source=EvidenceSource.CI_RUN,
            subject="nbhd-united-main-ci",
            occurred_at=stale_success_at,
            payload={
                "conclusion": "success",
                "head_sha": "b" * 40,
                "workflow": "CI",
                "run_attempt": 1,
            },
            fingerprint="ci-stale-rebind-probe",
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )
        train.refresh_from_db()
        train.head_sha = "b" * 40
        with patch("apps.steward.models.timezone.now", return_value=rebound_at):
            train.save(update_fields=["head_sha", "updated_at"])

        context, _ = self._client(runs=[])
        with patch("apps.steward.collectors.github.httpx.Client", return_value=context):
            result = github.collect_github()

        train.refresh_from_db()
        self.assertEqual(train.ci_binding_changed_at, rebound_at)
        self.assertEqual(train.phase, ReleaseTrain.Phase.PUSHED)
        self.assertEqual(result["train_advances"], 0)

    def test_ci_recovery_selects_eligible_train_beyond_cap(self):
        changed_at = timezone.now() - timedelta(minutes=1)
        trains = []
        for index in range(21):
            train = self._pushed_train(
                f"eligible-beyond-cap-{index}",
                sha=f"{index:040x}",
                changed_at=changed_at,
            )
            ReleaseTrain.objects.filter(pk=train.pk).update(ci_workflow="CI")
            trains.append(train)
        target = trains[-1]
        EvidenceEvent.objects.create(
            source=EvidenceSource.CI_RUN,
            subject="nbhd-united-main-ci",
            occurred_at=timezone.now(),
            payload={
                "conclusion": "success",
                "head_sha": target.head_sha,
                "workflow": "CI",
                "run_attempt": 1,
            },
            fingerprint="ci-eligible-beyond-cap",
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )

        advances, ambiguous = github._advance_ci_trains(
            repo="nbhd-united",
            product=TrackedItem.Product.NBHD_UNITED,
            workflow_names=frozenset({"CI"}),
        )

        target.refresh_from_db()
        trains[0].refresh_from_db()
        self.assertEqual(target.phase, ReleaseTrain.Phase.CI_GREEN)
        self.assertEqual(trains[0].phase, ReleaseTrain.Phase.PUSHED)
        self.assertEqual(advances, 1)
        self.assertFalse(ambiguous)

    def test_ci_recovery_caps_trains_and_uses_two_set_based_queries(self):
        changed_at = timezone.now() - timedelta(minutes=1)
        trains = []
        for index in range(21):
            train = self._pushed_train(
                f"bounded-{index}",
                sha=f"{index:040x}",
                changed_at=changed_at,
            )
            ReleaseTrain.objects.filter(pk=train.pk).update(ci_workflow="CI")
            trains.append(train)
        occurred_at = timezone.now()
        EvidenceEvent.objects.bulk_create(
            [
                EvidenceEvent(
                    source=EvidenceSource.CI_RUN,
                    subject="nbhd-united-main-ci",
                    occurred_at=occurred_at,
                    payload={
                        "conclusion": "success",
                        "head_sha": train.head_sha,
                        "workflow": "CI",
                        "run_attempt": 1,
                    },
                    fingerprint=f"ci-bounded-{index}",
                    trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                    provenance=EvidenceEvent.Provenance.COLLECTOR,
                )
                for index, train in enumerate(trains)
            ]
        )

        with (
            patch("apps.steward.collectors.github.advance_train") as advance,
            self.assertNumQueries(2),
        ):
            advances, ambiguous = github._advance_ci_trains(
                repo="nbhd-united",
                product=TrackedItem.Product.NBHD_UNITED,
                workflow_names=frozenset({"CI"}),
            )

        self.assertEqual(advances, 20)
        self.assertFalse(ambiguous)
        self.assertEqual(advance.call_count, 20)

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_ci_rerun_attempt_gets_distinct_evidence_and_missing_attempt_defaults_one(self):
        occurred_at = datetime.fromisoformat(TS.replace("Z", "+00:00"))
        train = self._pushed_train(
            "rerun",
            sha="a" * 40,
            changed_at=occurred_at - timedelta(minutes=1),
        )
        first_context, _ = self._client(
            runs=[
                workflow_run(
                    77,
                    conclusion="failure",
                    run_attempt=None,
                    name="provider/" + ("long-workflow-" * 30),
                )
            ]
        )
        with patch("apps.steward.collectors.github.httpx.Client", return_value=first_context):
            github.collect_github()
        train.refresh_from_db()
        self.assertEqual(train.phase, ReleaseTrain.Phase.PUSHED)

        second_context, _ = self._client(runs=[workflow_run(77, conclusion="success", run_attempt=2)])
        with patch("apps.steward.collectors.github.httpx.Client", return_value=second_context):
            result = github.collect_github()

        train.refresh_from_db()
        events = list(EvidenceEvent.objects.filter(source=EvidenceSource.CI_RUN).order_by("id"))
        self.assertEqual([event.payload["run_attempt"] for event in events], [1, 2])
        self.assertNotEqual(events[0].fingerprint, events[1].fingerprint)
        self.assertNotIn("provider", events[0].fingerprint)
        self.assertEqual(train.phase, ReleaseTrain.Phase.CI_GREEN)
        self.assertEqual(result["evidence"], 1)

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_long_tag_a_to_b_to_a_uses_monotonic_revision(self):
        tag_name = "release/" + ("provider-controlled-" * 20)
        first_context, _ = self._client(tags=[{"name": tag_name, "commit": {"sha": "b" * 40}}])
        with patch("apps.steward.collectors.github.httpx.Client", return_value=first_context):
            github.collect_github()
        second_context, _ = self._client(tags=[{"name": tag_name, "commit": {"sha": "c" * 40}}])
        with patch("apps.steward.collectors.github.httpx.Client", return_value=second_context):
            github.collect_github()
        third_context, _ = self._client(tags=[{"name": tag_name, "commit": {"sha": "b" * 40}}])
        with patch("apps.steward.collectors.github.httpx.Client", return_value=third_context):
            github.collect_github()

        events = list(EvidenceEvent.objects.filter(source=EvidenceSource.GITHUB_STATE))
        self.assertEqual(len(events), 3)
        self.assertTrue(all(len(event.fingerprint.rsplit(":", 1)[1]) == 24 for event in events))
        self.assertTrue(all(tag_name not in event.fingerprint for event in events))
        self.assertEqual(len({event.fingerprint for event in events}), 3)
        snapshot = GithubTagSnapshot.objects.get(repo="nbhd-united")
        self.assertEqual(snapshot.sha, "b" * 40)
        self.assertEqual(snapshot.revision, 2)

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_full_three_page_walk_restarts_at_top_dedupes_and_ignores_old_merges(self):
        collected_at = datetime(2026, 7, 31, 12, tzinfo=UTC)
        recent_merge_at = (collected_at - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        full_pages = {
            page: [
                pull_request(
                    page * 1000 + number,
                    state="closed",
                    updated_at=recent_merge_at,
                )
                for number in range(100)
            ]
            for page in range(1, 4)
        }
        full_pages[3][-1] = pull_request(
            3999,
            state="closed",
            merged_at=recent_merge_at,
            updated_at=recent_merge_at,
        )
        first_context, _ = self._client(all_pages=full_pages)
        with (
            patch("apps.steward.collectors.github.timezone.now", return_value=collected_at),
            patch(
                "apps.steward.collectors.github.httpx.Client",
                return_value=first_context,
            ),
        ):
            first = github.collect_github()

        new_merge_at = (collected_at - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        second_pages = {page: list(items) for page, items in full_pages.items()}
        second_pages[1][0] = pull_request(
            4000,
            state="closed",
            merged_at=new_merge_at,
            updated_at=new_merge_at,
        )
        second_context, second_client = self._client(all_pages=second_pages)
        with (
            patch("apps.steward.collectors.github.timezone.now", return_value=collected_at),
            patch(
                "apps.steward.collectors.github.httpx.Client",
                return_value=second_context,
            ),
        ):
            second = github.collect_github()

        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.GITHUB)
        self.assertEqual(status.last_error_class, "")
        self.assertIn("history_truncated", status.detail)

        old_merge_at = (collected_at - timedelta(days=8)).isoformat().replace("+00:00", "Z")
        old_context, _ = self._client(
            all_pages={
                1: [
                    pull_request(
                        5000,
                        state="closed",
                        merged_at=old_merge_at,
                        updated_at=old_merge_at,
                    )
                ]
            }
        )
        with (
            patch("apps.steward.collectors.github.timezone.now", return_value=collected_at),
            patch(
                "apps.steward.collectors.github.httpx.Client",
                return_value=old_context,
            ),
        ):
            old = github.collect_github()

        self.assertEqual((first["evidence"], second["evidence"], old["evidence"]), (1, 1, 0))
        self.assertEqual(EvidenceEvent.objects.filter(source=EvidenceSource.GITHUB_STATE).count(), 2)
        walked_pages = [
            call.kwargs["params"]["page"]
            for call in second_client.get.call_args_list
            if call.args[0].endswith("/pulls") and call.kwargs["params"]["state"] == "all"
        ]
        self.assertEqual(walked_pages, [1, 2, 3])

    def test_request_timeout_uses_remaining_budget_and_skips_below_two_seconds(self):
        client = MagicMock()
        client.get.return_value = FakeResponse({})
        deadline = github.CollectionDeadline(expires_at=100.0)
        with patch(
            "apps.steward.collectors.github.time.monotonic",
            side_effect=[97.5, 98.0],
        ):
            github._checked_get(client, deadline, "/resource")

        self.assertEqual(client.get.call_args.kwargs["timeout"], 2.5)

        skipped_client = MagicMock()
        with (
            patch("apps.steward.collectors.github.time.monotonic", return_value=99.0),
            self.assertRaises(github.CollectionDeadlineExceeded),
        ):
            github._checked_get(skipped_client, deadline, "/resource")
        skipped_client.get.assert_not_called()

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_deadline_is_rechecked_before_persistence(self):
        context, _ = self._client()
        deadline = MagicMock()
        deadline.check.side_effect = github.CollectionDeadlineExceeded
        collection = github.RepoCollection(
            mirrors=[],
            inputs=[],
            tags=[],
            workflow_names=frozenset(),
            history_truncated=False,
            incomplete=False,
            detail="",
        )
        with (
            patch(
                "apps.steward.collectors.github.httpx.Client",
                return_value=context,
            ),
            patch("apps.steward.collectors.github.CollectionDeadline", return_value=deadline),
            patch("apps.steward.collectors.github._collect_repo", return_value=collection),
            patch("apps.steward.collectors.github._persist_repo") as persist,
        ):
            result = github.collect_github()

        persist.assert_not_called()
        self.assertEqual(result["evidence"], 0)
        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.GITHUB)
        self.assertEqual(status.last_error_class, "CollectionDeadlineExceeded")

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_active_lease_skips_overlapping_run(self):
        CollectorStatus.objects.create(
            collector=CollectorStatus.Collector.GITHUB,
            held_until=timezone.now() + timedelta(minutes=5),
        )
        with (
            patch("apps.steward.collectors.github.httpx.Client") as client_class,
            patch.object(github.logger, "info") as log_info,
        ):
            result = github.collect_github()

        self.assertEqual(result, {"repos": 0, "pull_requests": 0, "evidence": 0, "train_advances": 0})
        client_class.assert_not_called()
        self.assertIn("lease already held", log_info.call_args.args[0])

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_repo_persistence_rolls_back_evidence_mirror_and_tag_together(self):
        context, _ = self._client(
            open_prs=[pull_request(3)],
            runs=[workflow_run(80)],
            tags=[{"name": "v1", "commit": {"sha": "c" * 40}}],
        )
        with (
            patch(
                "apps.steward.collectors.github.httpx.Client",
                return_value=context,
            ),
            patch(
                "apps.steward.collectors.github._advance_ci_trains",
                side_effect=RuntimeError("crash after evidence"),
            ),
        ):
            github.collect_github()

        self.assertFalse(RepoPullRequest.objects.filter(number=3).exists())
        self.assertFalse(EvidenceEvent.objects.exists())
        self.assertFalse(GithubTagSnapshot.objects.exists())

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_persistence_lock_timeout_records_failure_and_releases_lease(self):
        context, _ = self._client()
        with (
            patch("apps.steward.collectors.github.httpx.Client", return_value=context),
            patch(
                "apps.steward.collectors.github._persist_repo",
                side_effect=OperationalError("lock timeout"),
            ),
        ):
            result = github.collect_github()

        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.GITHUB)
        self.assertEqual(result["evidence"], 0)
        self.assertEqual(status.last_error_class, "OperationalError")
        self.assertIn("nbhd-united:OperationalError", status.detail)
        self.assertIsNone(status.held_until)

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    def test_repo_failure_isolated_from_later_repo(self):
        repos = [
            ("owner", "broken", TrackedItem.Product.NBHD_UNITED),
            ("owner", "healthy", TrackedItem.Product.SAUTAI),
        ]
        client = MagicMock()

        def get(path, params=None, timeout=None):
            params = params or {}
            if path == "/repos/owner/broken":
                raise RuntimeError("repo unavailable")
            if path == "/repos/owner/healthy":
                return FakeResponse({"default_branch": "main"})
            if path.endswith("/pulls"):
                return FakeResponse([])
            if path.endswith("/actions/runs"):
                return FakeResponse({"workflow_runs": []})
            if path.endswith("/tags"):
                return FakeResponse([])
            raise AssertionError(path)

        client.get.side_effect = get
        context = MagicMock()
        context.__enter__.return_value = client
        context.__exit__.return_value = False
        with (
            patch("apps.steward.collectors.github.GITHUB_REPOS", repos),
            patch("apps.steward.collectors.github.httpx.Client", return_value=context) as client_class,
        ):
            result = github.collect_github()

        self.assertEqual(result["repos"], 2)
        self.assertTrue(any(call.args[0] == "/repos/owner/healthy" for call in client.get.call_args_list))
        client_class.assert_called_once()
        self.assertEqual(client_class.call_args.kwargs["timeout"], github.GITHUB_TIMEOUT_SECONDS)
        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.GITHUB)
        self.assertEqual(status.last_error_class, "RuntimeError")
        self.assertIn("broken:RuntimeError", status.detail)

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_list_pagination_is_bounded_to_three_pages(self):
        hundred = [pull_request(number) for number in range(1, 101)]
        context, client = self._client(open_prs=hundred, all_pages={1: [], 2: [], 3: []})

        with patch("apps.steward.collectors.github.httpx.Client", return_value=context):
            github.collect_github()

        pages = [
            call.kwargs["params"]["page"]
            for call in client.get.call_args_list
            if call.args[0].endswith("/pulls") and call.kwargs["params"]["state"] == "open"
        ]
        self.assertEqual(pages, [1, 2, 3])
        self.assertEqual(RepoPullRequest.objects.count(), 100)
