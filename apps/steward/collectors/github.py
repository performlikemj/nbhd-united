from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from django.conf import settings
from django.utils import timezone

from apps.steward.models import (
    EvidenceEvent,
    EvidenceSource,
    ReleaseTrain,
    RepoPullRequest,
)
from apps.steward.sanitize import safe_text
from apps.steward.services import (
    EvidenceIngestInput,
    ingest_evidence_batch,
    stored_evidence_fingerprint,
)
from apps.steward.trains import advance_train

logger = logging.getLogger(__name__)

GITHUB_REPOS = [
    ("performlikemj", "nbhd-united", "nbhd_united"),
    ("performlikemj", "nbhd-ios", "nbhd_ios"),
    ("performlikemj", "sautai", "sautai"),
    ("performlikemj", "loanarmy", "academy_watch"),
]
GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_TIMEOUT_SECONDS = 15.0
GITHUB_PER_PAGE = 100
GITHUB_MAX_PAGES = 3


@dataclass(frozen=True)
class RepoSpec:
    owner: str
    repo: str
    product: str


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("GitHub timestamp is missing.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _response_json(response: httpx.Response) -> Any:
    response.raise_for_status()
    return response.json()


def _bounded_list(
    client: httpx.Client,
    path: str,
    *,
    params: dict[str, Any],
    key: str | None = None,
    max_pages: int = GITHUB_MAX_PAGES,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, min(max_pages, GITHUB_MAX_PAGES) + 1):
        payload = _response_json(
            client.get(
                path,
                params={
                    **params,
                    "per_page": GITHUB_PER_PAGE,
                    "page": page,
                },
            )
        )
        page_items = payload.get(key, []) if key else payload
        if not isinstance(page_items, list):
            raise ValueError("GitHub list response has an invalid shape.")
        items.extend(item for item in page_items if isinstance(item, dict))
        if len(page_items) < GITHUB_PER_PAGE:
            break
    return items


def _pr_state(payload: dict[str, Any]) -> str:
    if payload.get("merged_at"):
        return RepoPullRequest.State.MERGED
    if payload.get("state") == "open":
        return RepoPullRequest.State.OPEN
    return RepoPullRequest.State.CLOSED


def _pr_mirror(repo: str, payload: dict[str, Any], *, synced_at: datetime) -> RepoPullRequest:
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    head = payload.get("head") if isinstance(payload.get("head"), dict) else {}
    author = safe_text(str(user.get("login") or "unknown"), 60)
    return RepoPullRequest(
        repo=repo,
        number=int(payload["number"]),
        title=safe_text(str(payload.get("title") or ""), 140),
        author=author,
        draft=bool(payload.get("draft")),
        state=_pr_state(payload),
        opened_at=_parse_timestamp(payload.get("created_at")),
        last_activity_at=_parse_timestamp(payload.get("updated_at")),
        is_dependabot=author.casefold() == "dependabot[bot]",
        head_ref=safe_text(str(head.get("ref") or ""), 120),
        synced_at=synced_at,
    )


def _collect_repo(
    client: httpx.Client,
    spec: RepoSpec,
    *,
    collected_at: datetime,
    existing_prs: dict[tuple[str, int], RepoPullRequest],
) -> tuple[list[RepoPullRequest], list[EvidenceIngestInput]]:
    base = f"/repos/{spec.owner}/{spec.repo}"
    repo_info = _response_json(client.get(base))
    default_branch = str(repo_info.get("default_branch") or "main")

    open_prs = _bounded_list(
        client,
        f"{base}/pulls",
        params={"state": "open", "sort": "updated", "direction": "desc"},
    )
    recent_all = _bounded_list(
        client,
        f"{base}/pulls",
        params={"state": "all", "sort": "updated", "direction": "desc"},
        max_pages=1,
    )
    merged_payloads: list[tuple[RepoPullRequest, dict[str, Any]]] = []
    mirrors_by_number: dict[int, RepoPullRequest] = {}
    payloads_by_number: dict[int, dict[str, Any]] = {}
    for payload in [*open_prs, *recent_all]:
        try:
            mirror = _pr_mirror(spec.repo, payload, synced_at=collected_at)
        except (KeyError, TypeError, ValueError):
            logger.warning("Steward GitHub collector skipped malformed PR repo=%s", spec.repo)
            continue
        mirrors_by_number[mirror.number] = mirror
        payloads_by_number[mirror.number] = payload

    for number, mirror in mirrors_by_number.items():
        previous = existing_prs.get((spec.repo, number))
        if (
            mirror.state == RepoPullRequest.State.MERGED
            and previous is not None
            and previous.state != RepoPullRequest.State.MERGED
        ):
            merged_payloads.append((mirror, payloads_by_number[number]))

    inputs = [
        EvidenceIngestInput(
            source=EvidenceSource.GITHUB_STATE,
            subject=f"repo:{spec.repo}",
            occurred_at=_parse_timestamp(payload.get("merged_at")),
            payload={
                "number": mirror.number,
                "title": safe_text(mirror.title, 100),
                "merged_at": payload["merged_at"],
            },
            fingerprint=f"gh-pr-merged:{spec.repo}:{mirror.number}",
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )
        for mirror, payload in merged_payloads
    ]

    runs = _bounded_list(
        client,
        f"{base}/actions/runs",
        params={"branch": default_branch},
        key="workflow_runs",
    )
    for run in runs:
        if run.get("status") != "completed" or run.get("head_branch") != default_branch:
            continue
        run_id = run.get("id")
        conclusion = run.get("conclusion")
        if not isinstance(run_id, int) or not isinstance(conclusion, str):
            continue
        inputs.append(
            EvidenceIngestInput(
                source=EvidenceSource.CI_RUN,
                subject=f"{spec.repo}-main-ci",
                occurred_at=_parse_timestamp(run.get("updated_at") or run.get("run_started_at")),
                payload={
                    "conclusion": conclusion,
                    "head_sha": str(run.get("head_sha") or ""),
                    "workflow": safe_text(str(run.get("name") or ""), 100),
                },
                fingerprint=f"gh-ci:{spec.repo}:{run_id}",
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
        )

    tags = _bounded_list(
        client,
        f"{base}/tags",
        params={},
    )
    for tag in tags:
        commit = tag.get("commit") if isinstance(tag.get("commit"), dict) else {}
        name = tag.get("name")
        sha = commit.get("sha")
        if not isinstance(name, str) or not name or not isinstance(sha, str):
            continue
        inputs.append(
            EvidenceIngestInput(
                source=EvidenceSource.GITHUB_STATE,
                subject=f"repo:{spec.repo}",
                occurred_at=collected_at,
                payload={
                    "tag": safe_text(name, 100),
                    "sha": sha,
                },
                fingerprint=f"gh-tag:{spec.repo}:{name}",
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
        )
    return list(mirrors_by_number.values()), inputs


def _new_inputs(inputs: list[EvidenceIngestInput]) -> list[EvidenceIngestInput]:
    fingerprints = [stored_evidence_fingerprint(item.source, item.fingerprint) for item in inputs]
    existing = set(
        EvidenceEvent.objects.filter(fingerprint__in=fingerprints).values_list(
            "fingerprint",
            flat=True,
        )
    )
    return [item for item in inputs if stored_evidence_fingerprint(item.source, item.fingerprint) not in existing]


def collect_github() -> dict[str, int]:
    """Collect bounded read-only GitHub state and deterministic transition facts."""
    token = str(getattr(settings, "STEWARD_GITHUB_TOKEN", "") or "").strip()
    if not token:
        logger.info("Steward GitHub collector disabled: STEWARD_GITHUB_TOKEN is unset")
        return {"repos": 0, "pull_requests": 0, "evidence": 0, "train_advances": 0}

    collected_at = timezone.now()
    existing_prs = {
        (pr.repo, pr.number): pr
        for pr in RepoPullRequest.objects.filter(repo__in=[repo for _, repo, _ in GITHUB_REPOS])
    }
    mirrors: list[RepoPullRequest] = []
    inputs: list[EvidenceIngestInput] = []
    with httpx.Client(
        base_url=GITHUB_API_BASE_URL,
        timeout=GITHUB_TIMEOUT_SECONDS,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    ) as client:
        for owner, repo, product in GITHUB_REPOS:
            repo_mirrors, repo_inputs = _collect_repo(
                client,
                RepoSpec(owner, repo, product),
                collected_at=collected_at,
                existing_prs=existing_prs,
            )
            mirrors.extend(repo_mirrors)
            inputs.extend(repo_inputs)

    if mirrors:
        RepoPullRequest.objects.bulk_create(
            mirrors,
            update_conflicts=True,
            update_fields=[
                "title",
                "author",
                "draft",
                "state",
                "opened_at",
                "last_activity_at",
                "is_dependabot",
                "head_ref",
                "synced_at",
            ],
            unique_fields=["repo", "number"],
        )

    inputs = _new_inputs(inputs)
    results = ingest_evidence_batch(inputs, now=collected_at)
    advances = 0
    product_by_repo = {repo: product for _, repo, product in GITHUB_REPOS}
    for item, result in zip(inputs, results, strict=True):
        if result.created and item.source == EvidenceSource.CI_RUN and item.payload.get("conclusion") == "success":
            repo = item.subject.removesuffix("-main-ci")
            # Conservative v0 boundary: GitHub advances only pushed→ci_green.
            # Merges and tags remain evidence for MJ and the digest.
            for train in ReleaseTrain.objects.filter(
                product=product_by_repo.get(repo),
                phase=ReleaseTrain.Phase.PUSHED,
            ):
                advance_train(
                    train,
                    ReleaseTrain.Phase.CI_GREEN,
                    evidence=result.event,
                    provenance=EvidenceEvent.Provenance.COLLECTOR,
                )
                advances += 1
    return {
        "repos": len(GITHUB_REPOS),
        "pull_requests": len(mirrors),
        "evidence": sum(result.created for result in results),
        "train_advances": advances,
    }
