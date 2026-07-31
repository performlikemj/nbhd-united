from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from django.conf import settings
from django.utils import timezone

from apps.steward.collectors.status import collector_failed, collector_succeeded
from apps.steward.models import (
    AlertState,
    CollectorStatus,
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
GITHUB_MAX_RECONCILES = 20
GITHUB_SOFT_DEADLINE_SECONDS = 240.0


@dataclass(frozen=True)
class RepoSpec:
    owner: str
    repo: str
    product: str


@dataclass(frozen=True)
class RepoCollection:
    mirrors: list[RepoPullRequest]
    inputs: list[EvidenceIngestInput]
    truncated: bool
    detail: str


def _fingerprint_hash(*parts: object) -> str:
    material = json.dumps(parts, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


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


def _updated_prs_since(
    client: httpx.Client,
    path: str,
    *,
    watermark: datetime | None,
) -> tuple[list[dict[str, Any]], bool]:
    items: list[dict[str, Any]] = []
    reached_watermark = False
    last_page_full = False
    for page in range(1, GITHUB_MAX_PAGES + 1):
        page_items = _response_json(
            client.get(
                path,
                params={
                    "state": "all",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": GITHUB_PER_PAGE,
                    "page": page,
                },
            )
        )
        if not isinstance(page_items, list):
            raise ValueError("GitHub pull request response has an invalid shape.")
        last_page_full = len(page_items) == GITHUB_PER_PAGE
        for item in page_items:
            if not isinstance(item, dict):
                continue
            try:
                updated_at = _parse_timestamp(item.get("updated_at"))
            except ValueError:
                continue
            if watermark is not None and updated_at <= watermark:
                reached_watermark = True
                break
            items.append(item)
        if reached_watermark or not last_page_full:
            break
    return items, bool(last_page_full and not reached_watermark)


def _watermark_state(repo: str) -> AlertState:
    state, _ = AlertState.objects.get_or_create(
        fingerprint=f"github-pr-watermark:{repo}",
    )
    return state


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
    existing_prs: dict[int, RepoPullRequest],
) -> RepoCollection:
    base = f"/repos/{spec.owner}/{spec.repo}"
    repo_info = _response_json(client.get(base))
    default_branch = str(repo_info.get("default_branch") or "main")

    open_prs = _bounded_list(
        client,
        f"{base}/pulls",
        params={"state": "open", "sort": "updated", "direction": "desc"},
    )
    watermark_state = _watermark_state(spec.repo)
    recent_all, history_truncated = _updated_prs_since(
        client,
        f"{base}/pulls",
        watermark=watermark_state.last_sent_at,
    )
    newest_update = watermark_state.last_sent_at
    for payload in recent_all:
        try:
            updated_at = _parse_timestamp(payload.get("updated_at"))
        except ValueError:
            continue
        if newest_update is None or updated_at > newest_update:
            newest_update = updated_at

    open_numbers = {int(payload["number"]) for payload in open_prs if isinstance(payload.get("number"), int)}
    reconcile_candidates = sorted(
        (
            previous
            for previous in existing_prs.values()
            if previous.state == RepoPullRequest.State.OPEN and previous.number not in open_numbers
        ),
        key=lambda previous: (previous.synced_at, previous.number),
    )
    reconcile_deferred = max(0, len(reconcile_candidates) - GITHUB_MAX_RECONCILES)
    reconciled: list[dict[str, Any]] = []
    reconcile_errors: list[str] = []
    for previous in reconcile_candidates[:GITHUB_MAX_RECONCILES]:
        try:
            payload = _response_json(client.get(f"{base}/pulls/{previous.number}"))
            if not isinstance(payload, dict):
                raise ValueError("GitHub pull request response has an invalid shape.")
            reconciled.append(payload)
        except Exception as exc:
            reconcile_errors.append(type(exc).__name__)
            logger.warning(
                "Steward GitHub PR reconcile failed repo=%s number=%s error_class=%s",
                spec.repo,
                previous.number,
                type(exc).__name__,
            )

    merged_payloads: list[tuple[RepoPullRequest, dict[str, Any]]] = []
    mirrors_by_number: dict[int, RepoPullRequest] = {}
    payloads_by_number: dict[int, dict[str, Any]] = {}
    for payload in [*open_prs, *recent_all, *reconciled]:
        try:
            mirror = _pr_mirror(spec.repo, payload, synced_at=collected_at)
        except (KeyError, TypeError, ValueError):
            logger.warning("Steward GitHub collector skipped malformed PR repo=%s", spec.repo)
            continue
        mirrors_by_number[mirror.number] = mirror
        payloads_by_number[mirror.number] = payload

    for number, mirror in mirrors_by_number.items():
        previous = existing_prs.get(number)
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
                "merged_at": payload["merged_at"],
            },
            fingerprint=f"gh-pr-merged:{_fingerprint_hash(spec.repo, mirror.number)}",
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
        run_attempt = run.get("run_attempt", 1)
        if not isinstance(run_attempt, int) or isinstance(run_attempt, bool) or run_attempt < 1:
            run_attempt = 1
        try:
            occurred_at = _parse_timestamp(run.get("updated_at") or run.get("run_started_at"))
        except ValueError:
            continue
        inputs.append(
            EvidenceIngestInput(
                source=EvidenceSource.CI_RUN,
                subject=f"{spec.repo}-main-ci",
                occurred_at=occurred_at,
                payload={
                    "conclusion": conclusion,
                    "head_sha": str(run.get("head_sha") or ""),
                    "workflow": safe_text(str(run.get("name") or ""), 100),
                    "run_attempt": run_attempt,
                },
                fingerprint=f"gh-ci:{_fingerprint_hash(spec.repo, run_id, run_attempt)}",
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
        )

    tags = _bounded_list(client, f"{base}/tags", params={})
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
                fingerprint=f"gh-tag:{_fingerprint_hash(spec.repo, name, sha)}",
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
        )

    if newest_update != watermark_state.last_sent_at:
        watermark_state.last_sent_at = newest_update
        watermark_state.save(update_fields=["last_sent_at"])
    detail_parts = []
    if history_truncated:
        detail_parts.append(f"{spec.repo}:history_truncated")
    if reconcile_deferred:
        detail_parts.append(f"{spec.repo}:reconcile_deferred={reconcile_deferred}")
    if reconcile_errors:
        detail_parts.append(f"{spec.repo}:reconcile_errors={len(reconcile_errors)}")
    return RepoCollection(
        mirrors=list(mirrors_by_number.values()),
        inputs=inputs,
        truncated=history_truncated or bool(reconcile_deferred) or bool(reconcile_errors),
        detail=";".join(detail_parts),
    )


def _new_inputs(inputs: list[EvidenceIngestInput]) -> list[EvidenceIngestInput]:
    fingerprints = [stored_evidence_fingerprint(item.source, item.fingerprint) for item in inputs]
    existing = set(
        EvidenceEvent.objects.filter(fingerprint__in=fingerprints).values_list(
            "fingerprint",
            flat=True,
        )
    )
    return [item for item in inputs if stored_evidence_fingerprint(item.source, item.fingerprint) not in existing]


def _upsert_mirrors(mirrors: list[RepoPullRequest]) -> None:
    if not mirrors:
        return
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


def _ingest_repo_inputs(
    inputs: list[EvidenceIngestInput],
    *,
    collected_at: datetime,
    product: str,
) -> tuple[int, int]:
    new_inputs = _new_inputs(inputs)
    results = ingest_evidence_batch(new_inputs, now=collected_at)
    advances = 0
    for item, result in zip(new_inputs, results, strict=True):
        if not result.created or item.source != EvidenceSource.CI_RUN or item.payload.get("conclusion") != "success":
            continue
        head_sha = item.payload.get("head_sha")
        if not isinstance(head_sha, str) or len(head_sha) != 40:
            continue
        for train in ReleaseTrain.objects.filter(
            product=product,
            phase=ReleaseTrain.Phase.PUSHED,
            head_sha=head_sha,
            phase_changed_at__lte=item.occurred_at,
        ):
            advance_train(
                train,
                ReleaseTrain.Phase.CI_GREEN,
                evidence=result.event,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
            advances += 1
    return sum(result.created for result in results), advances


def collect_github() -> dict[str, int]:
    """Collect bounded read-only GitHub state with per-repository isolation."""
    collected_at = timezone.now()
    token = str(getattr(settings, "STEWARD_GITHUB_TOKEN", "") or "").strip()
    if not token:
        logger.info("Steward GitHub collector disabled: STEWARD_GITHUB_TOKEN is unset")
        collector_failed(
            CollectorStatus.Collector.GITHUB,
            attempted_at=collected_at,
            error_class="not_configured",
            detail="STEWARD_GITHUB_TOKEN unset",
        )
        return {"repos": 0, "pull_requests": 0, "evidence": 0, "train_advances": 0}

    started_at = time.monotonic()
    totals = {
        "repos": 0,
        "pull_requests": 0,
        "evidence": 0,
        "train_advances": 0,
    }
    failures: list[str] = []
    truncations: list[str] = []
    try:
        with httpx.Client(
            base_url=GITHUB_API_BASE_URL,
            timeout=GITHUB_TIMEOUT_SECONDS,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as client:
            for index, (owner, repo, product) in enumerate(GITHUB_REPOS):
                if index and time.monotonic() - started_at >= GITHUB_SOFT_DEADLINE_SECONDS:
                    truncations.append(f"deadline_after={totals['repos']}")
                    break
                totals["repos"] += 1
                try:
                    existing_prs = {pr.number: pr for pr in RepoPullRequest.objects.filter(repo=repo)}
                    collection = _collect_repo(
                        client,
                        RepoSpec(owner, repo, product),
                        collected_at=collected_at,
                        existing_prs=existing_prs,
                    )
                    _upsert_mirrors(collection.mirrors)
                    evidence, advances = _ingest_repo_inputs(
                        collection.inputs,
                        collected_at=collected_at,
                        product=product,
                    )
                    totals["pull_requests"] += len(collection.mirrors)
                    totals["evidence"] += evidence
                    totals["train_advances"] += advances
                    if collection.truncated:
                        truncations.append(collection.detail or f"{repo}:truncated")
                except Exception as exc:
                    failures.append(f"{repo}:{type(exc).__name__}")
                    logger.warning(
                        "Steward GitHub repo collection failed repo=%s error_class=%s",
                        repo,
                        type(exc).__name__,
                    )
    except Exception as exc:
        collector_failed(
            CollectorStatus.Collector.GITHUB,
            attempted_at=collected_at,
            error_class=type(exc).__name__,
            detail="client setup failed",
        )
        raise

    if failures:
        collector_failed(
            CollectorStatus.Collector.GITHUB,
            attempted_at=collected_at,
            error_class=failures[0].split(":", 1)[1],
            detail=";".join([*failures, *truncations]),
        )
    elif truncations:
        collector_failed(
            CollectorStatus.Collector.GITHUB,
            attempted_at=collected_at,
            error_class="truncated",
            detail=";".join(truncations),
        )
    else:
        collector_succeeded(
            CollectorStatus.Collector.GITHUB,
            attempted_at=collected_at,
            detail=f"repos={totals['repos']}",
        )
    return totals
