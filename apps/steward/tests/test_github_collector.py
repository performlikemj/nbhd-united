from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.steward.collectors import github
from apps.steward.models import (
    AlertState,
    CollectorStatus,
    EvidenceEvent,
    EvidenceSource,
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

        def get(path, params=None):
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
    def test_pr_watermark_reaches_second_page_and_merge_evidence_has_no_title(self):
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
        watermark = AlertState.objects.get(fingerprint="github-pr-watermark:nbhd-united")
        self.assertEqual(watermark.last_sent_at, datetime.fromisoformat(TS2.replace("Z", "+00:00")))

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
    def test_long_tag_is_hashed_and_force_move_reemits(self):
        tag_name = "release/" + ("provider-controlled-" * 20)
        first_context, _ = self._client(tags=[{"name": tag_name, "commit": {"sha": "b" * 40}}])
        with patch("apps.steward.collectors.github.httpx.Client", return_value=first_context):
            github.collect_github()
        second_context, _ = self._client(tags=[{"name": tag_name, "commit": {"sha": "c" * 40}}])
        with patch("apps.steward.collectors.github.httpx.Client", return_value=second_context):
            github.collect_github()

        events = list(EvidenceEvent.objects.filter(source=EvidenceSource.GITHUB_STATE))
        self.assertEqual(len(events), 2)
        self.assertTrue(all(len(event.fingerprint.rsplit(":", 1)[1]) == 24 for event in events))
        self.assertTrue(all(tag_name not in event.fingerprint for event in events))
        self.assertNotEqual(events[0].fingerprint, events[1].fingerprint)

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    def test_repo_failure_isolated_and_soft_deadline_stops_between_repos(self):
        repos = [
            ("owner", "broken", TrackedItem.Product.NBHD_UNITED),
            ("owner", "healthy", TrackedItem.Product.SAUTAI),
            ("owner", "deferred", TrackedItem.Product.ACADEMY_WATCH),
        ]
        client = MagicMock()

        def get(path, params=None):
            params = params or {}
            if path == "/repos/owner/broken":
                raise RuntimeError("repo unavailable")
            if path in {"/repos/owner/healthy", "/repos/owner/deferred"}:
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
            patch("apps.steward.collectors.github.time.monotonic", side_effect=[0.0, 1.0, 241.0]),
        ):
            result = github.collect_github()

        self.assertEqual(result["repos"], 2)
        self.assertTrue(any(call.args[0] == "/repos/owner/healthy" for call in client.get.call_args_list))
        self.assertFalse(any(call.args[0] == "/repos/owner/deferred" for call in client.get.call_args_list))
        client_class.assert_called_once()
        self.assertEqual(client_class.call_args.kwargs["timeout"], github.GITHUB_TIMEOUT_SECONDS)
        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.GITHUB)
        self.assertEqual(status.last_error_class, "RuntimeError")
        self.assertIn("broken:RuntimeError", status.detail)
        self.assertIn("deadline_after=2", status.detail)

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
