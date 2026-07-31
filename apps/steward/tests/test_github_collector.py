from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.steward.collectors.github import collect_github
from apps.steward.models import (
    EvidenceEvent,
    EvidenceSource,
    ReleaseTrain,
    RepoPullRequest,
    TrackedItem,
)
from apps.steward.services import stored_evidence_fingerprint
from apps.steward.trains import advance_train, open_train

TEST_REPOS = [("owner", "nbhd-united", TrackedItem.Product.NBHD_UNITED)]
TS = "2026-07-30T12:00:00Z"


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
):
    return {
        "number": number,
        "title": title,
        "user": {"login": author},
        "draft": draft,
        "state": state,
        "created_at": TS,
        "updated_at": TS,
        "merged_at": merged_at,
        "head": {"ref": f"feature-{number}"},
    }


def workflow_run(run_id: int, *, conclusion="success"):
    return {
        "id": run_id,
        "status": "completed",
        "conclusion": conclusion,
        "head_branch": "main",
        "head_sha": "a" * 40,
        "name": "CI",
        "updated_at": TS,
    }


class GitHubCollectorTests(TestCase):
    def _client(self, *, open_prs=None, all_prs=None, runs=None, tags=None):
        open_prs = open_prs or []
        all_prs = all_prs if all_prs is not None else open_prs
        runs = runs or []
        tags = tags or []
        client = MagicMock()

        def get(path, params=None):
            params = params or {}
            if path == "/repos/owner/nbhd-united":
                return FakeResponse({"default_branch": "main"})
            if path.endswith("/pulls"):
                return FakeResponse(open_prs if params["state"] == "open" else all_prs)
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
    def test_disabled_without_token_never_opens_http_client(self, client_class):
        self.assertEqual(collect_github()["repos"], 0)
        client_class.assert_not_called()

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_mirror_upsert_close_detection_dependabot_and_merge_idempotency(self):
        first_context, _ = self._client(
            open_prs=[
                pull_request(1),
                pull_request(2, author="dependabot[bot]", draft=True),
            ]
        )
        with patch(
            "apps.steward.collectors.github.httpx.Client",
            return_value=first_context,
        ):
            collect_github()

        self.assertTrue(RepoPullRequest.objects.get(number=2).is_dependabot)
        second_context, _ = self._client(
            open_prs=[],
            all_prs=[
                pull_request(1, state="closed", merged_at=TS, title="Merged\x00 title"),
                pull_request(2, state="closed"),
            ],
        )
        with patch(
            "apps.steward.collectors.github.httpx.Client",
            return_value=second_context,
        ):
            result = collect_github()
        self.assertEqual(result["evidence"], 1)
        self.assertEqual(RepoPullRequest.objects.get(number=1).state, RepoPullRequest.State.MERGED)
        self.assertEqual(RepoPullRequest.objects.get(number=2).state, RepoPullRequest.State.CLOSED)
        event = EvidenceEvent.objects.get(
            fingerprint=stored_evidence_fingerprint(
                EvidenceSource.GITHUB_STATE,
                "gh-pr-merged:nbhd-united:1",
            )
        )
        self.assertNotIn("\x00", event.payload["title"])

        third_context, _ = self._client(open_prs=[], all_prs=[])
        with patch(
            "apps.steward.collectors.github.httpx.Client",
            return_value=third_context,
        ):
            collect_github()
        self.assertEqual(EvidenceEvent.objects.count(), 1)

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_ci_subject_and_success_auto_advance(self):
        train = open_train(
            product=TrackedItem.Product.NBHD_UNITED,
            version_string="2026.7.31",
        )
        train = advance_train(
            train,
            ReleaseTrain.Phase.PUSHED,
            provenance=EvidenceEvent.Provenance.MJ,
        )
        context, _ = self._client(runs=[workflow_run(55)])

        with patch(
            "apps.steward.collectors.github.httpx.Client",
            return_value=context,
        ):
            result = collect_github()

        event = EvidenceEvent.objects.get(
            fingerprint=stored_evidence_fingerprint(
                EvidenceSource.CI_RUN,
                "gh-ci:nbhd-united:55",
            )
        )
        self.assertEqual(event.subject, "nbhd-united-main-ci")
        train.refresh_from_db()
        self.assertEqual(train.phase, ReleaseTrain.Phase.CI_GREEN)
        self.assertEqual(result["train_advances"], 1)

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_tag_event_is_fingerprint_idempotent(self):
        tags = [{"name": "v2.0.0", "commit": {"sha": "b" * 40}}]
        first_context, _ = self._client(tags=tags)
        with patch(
            "apps.steward.collectors.github.httpx.Client",
            return_value=first_context,
        ):
            collect_github()
        second_context, _ = self._client(tags=tags)
        with patch(
            "apps.steward.collectors.github.httpx.Client",
            return_value=second_context,
        ):
            collect_github()

        self.assertEqual(
            EvidenceEvent.objects.filter(
                fingerprint=stored_evidence_fingerprint(
                    EvidenceSource.GITHUB_STATE,
                    "gh-tag:nbhd-united:v2.0.0",
                )
            ).count(),
            1,
        )

    @override_settings(STEWARD_GITHUB_TOKEN="github_pat_FAKE_TEST_ONLY")
    @patch("apps.steward.collectors.github.GITHUB_REPOS", TEST_REPOS)
    def test_list_pagination_is_bounded_to_three_pages(self):
        client = MagicMock()
        hundred = [pull_request(number) for number in range(1, 101)]

        def get(path, params=None):
            params = params or {}
            if path == "/repos/owner/nbhd-united":
                return FakeResponse({"default_branch": "main"})
            if path.endswith("/pulls"):
                if params["state"] == "all":
                    return FakeResponse([])
                return FakeResponse(hundred)
            if path.endswith("/actions/runs"):
                return FakeResponse({"workflow_runs": []})
            if path.endswith("/tags"):
                return FakeResponse([])
            raise AssertionError(path)

        client.get.side_effect = get
        context = MagicMock()
        context.__enter__.return_value = client
        context.__exit__.return_value = False
        with patch(
            "apps.steward.collectors.github.httpx.Client",
            return_value=context,
        ):
            collect_github()

        pages = [
            call.kwargs["params"]["page"]
            for call in client.get.call_args_list
            if call.args[0].endswith("/pulls") and call.kwargs["params"]["state"] == "open"
        ]
        self.assertEqual(pages, [1, 2, 3])
        self.assertEqual(RepoPullRequest.objects.count(), 100)
