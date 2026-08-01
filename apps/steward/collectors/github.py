from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from django.conf import settings
from django.db import transaction
from django.db.models import Case, CharField, Exists, F, OuterRef, Q, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from apps.steward.collectors.status import (
    acquire_collector_lease,
    collector_failed,
    collector_succeeded,
    release_collector_lease,
    set_persistence_timeouts,
)
from apps.steward.models import (
    CollectorStatus,
    EvidenceEvent,
    EvidenceSource,
    GithubTagSnapshot,
    ReleaseTrain,
    RepoPullRequest,
)
from apps.steward.sanitize import safe_text
from apps.steward.services import (
    EvidenceIngestInput,
    ingest_evidence_batch,
    stored_evidence_fingerprint,
)
from apps.steward.trains import advance_train, ci_evidence_epoch

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
GITHUB_RECOVERY_MAX_TRAINS = 20
GITHUB_OVERALL_DEADLINE_SECONDS = 240.0
GITHUB_REPO_DEADLINE_SECONDS = 90.0
GITHUB_RECENT_MERGE_DAYS = 7


@dataclass(frozen=True)
class RepoSpec:
    owner: str
    repo: str
    product: str


@dataclass(frozen=True)
class RepoCollection:
    mirrors: list[RepoPullRequest]
    inputs: list[EvidenceIngestInput]
    tags: list[tuple[str, str]]
    workflow_names: frozenset[str]
    history_truncated: bool
    incomplete: bool
    detail: str


@dataclass(frozen=True)
class CollectionDeadline:
    expires_at: float

    def remaining(self) -> float:
        return self.expires_at - time.monotonic()

    def check(self) -> None:
        if self.remaining() < 2.0:
            raise CollectionDeadlineExceeded

    def request_timeout(self) -> float:
        remaining = self.remaining()
        if remaining < 2.0:
            raise CollectionDeadlineExceeded
        return min(GITHUB_TIMEOUT_SECONDS, remaining)


class CollectionDeadlineExceeded(Exception):
    pass


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


def _checked_get(
    client: httpx.Client,
    deadline: CollectionDeadline,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    response = client.get(
        path,
        params=params,
        timeout=deadline.request_timeout(),
    )
    deadline.check()
    return response


def _bounded_list(
    client: httpx.Client,
    deadline: CollectionDeadline,
    path: str,
    *,
    params: dict[str, Any],
    key: str | None = None,
    max_pages: int = GITHUB_MAX_PAGES,
) -> tuple[list[dict[str, Any]], bool]:
    items: list[dict[str, Any]] = []
    for page in range(1, min(max_pages, GITHUB_MAX_PAGES) + 1):
        try:
            payload = _response_json(
                _checked_get(
                    client,
                    deadline,
                    path,
                    params={
                        **params,
                        "per_page": GITHUB_PER_PAGE,
                        "page": page,
                    },
                )
            )
        except CollectionDeadlineExceeded:
            return items, True
        page_items = payload.get(key, []) if key else payload
        if not isinstance(page_items, list):
            raise ValueError("GitHub list response has an invalid shape.")
        items.extend(item for item in page_items if isinstance(item, dict))
        if len(page_items) < GITHUB_PER_PAGE:
            break
    return items, False


def _updated_prs(
    client: httpx.Client,
    deadline: CollectionDeadline,
    path: str,
) -> tuple[list[dict[str, Any]], bool, bool]:
    items: list[dict[str, Any]] = []
    last_page_full = False
    for page in range(1, GITHUB_MAX_PAGES + 1):
        try:
            page_items = _response_json(
                _checked_get(
                    client,
                    deadline,
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
        except CollectionDeadlineExceeded:
            return items, False, True
        if not isinstance(page_items, list):
            raise ValueError("GitHub pull request response has an invalid shape.")
        last_page_full = len(page_items) == GITHUB_PER_PAGE
        items.extend(item for item in page_items if isinstance(item, dict))
        if not last_page_full:
            break
    return items, last_page_full, False


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
    deadline: CollectionDeadline,
) -> RepoCollection:
    base = f"/repos/{spec.owner}/{spec.repo}"
    detail_parts: list[str] = []
    deadline_hit = False
    history_truncated = False
    open_prs: list[dict[str, Any]] = []
    recent_all: list[dict[str, Any]] = []
    reconciled: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    tags: list[tuple[str, str]] = []
    reconcile_deferred = 0
    reconcile_errors: list[str] = []

    try:
        repo_info = _response_json(_checked_get(client, deadline, base))
    except CollectionDeadlineExceeded:
        deadline_hit = True
        repo_info = {}
    default_branch = str(repo_info.get("default_branch") or "main")

    if not deadline_hit:
        open_prs, deadline_hit = _bounded_list(
            client,
            deadline,
            f"{base}/pulls",
            params={"state": "open", "sort": "updated", "direction": "desc"},
        )
    if not deadline_hit:
        recent_all, history_truncated, deadline_hit = _updated_prs(
            client,
            deadline,
            f"{base}/pulls",
        )

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
    for previous in reconcile_candidates[:GITHUB_MAX_RECONCILES] if not deadline_hit else []:
        try:
            payload = _response_json(
                _checked_get(
                    client,
                    deadline,
                    f"{base}/pulls/{previous.number}",
                )
            )
            if not isinstance(payload, dict):
                raise ValueError("GitHub pull request response has an invalid shape.")
            reconciled.append(payload)
        except CollectionDeadlineExceeded:
            deadline_hit = True
            break
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
        if mirror.state != RepoPullRequest.State.MERGED:
            continue
        payload = payloads_by_number[number]
        try:
            merged_at = _parse_timestamp(payload.get("merged_at"))
        except ValueError:
            continue
        if merged_at >= collected_at - timedelta(days=GITHUB_RECENT_MERGE_DAYS):
            merged_payloads.append((mirror, payload))

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

    if not deadline_hit:
        runs, deadline_hit = _bounded_list(
            client,
            deadline,
            f"{base}/actions/runs",
            params={"branch": default_branch},
            key="workflow_runs",
        )
    workflow_names = frozenset(
        safe_text(str(run.get("name")), 140)
        for run in runs
        if run.get("head_branch") == default_branch and isinstance(run.get("name"), str) and run.get("name")
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
                    "workflow": safe_text(str(run.get("name") or ""), 140),
                    "run_attempt": run_attempt,
                },
                fingerprint=f"gh-ci:{_fingerprint_hash(spec.repo, run_id, run_attempt)}",
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
        )

    tag_payloads: list[dict[str, Any]] = []
    if not deadline_hit:
        tag_payloads, deadline_hit = _bounded_list(
            client,
            deadline,
            f"{base}/tags",
            params={},
        )
    for tag in tag_payloads:
        commit = tag.get("commit") if isinstance(tag.get("commit"), dict) else {}
        name = tag.get("name")
        sha = commit.get("sha")
        if not isinstance(name, str) or not name or not isinstance(sha, str):
            continue
        tags.append((name, sha))

    if history_truncated:
        detail_parts.append(f"{spec.repo}:history_truncated")
    if deadline_hit:
        detail_parts.append(f"{spec.repo}:deadline")
    if reconcile_deferred:
        detail_parts.append(f"{spec.repo}:reconcile_deferred={reconcile_deferred}")
    if reconcile_errors:
        detail_parts.append(f"{spec.repo}:reconcile_errors={len(reconcile_errors)}")
    return RepoCollection(
        mirrors=list(mirrors_by_number.values()),
        inputs=inputs,
        tags=tags,
        workflow_names=workflow_names,
        history_truncated=history_truncated,
        incomplete=deadline_hit or bool(reconcile_deferred) or bool(reconcile_errors),
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


def _tag_inputs(
    repo: str,
    tags: list[tuple[str, str]],
    *,
    collected_at: datetime,
) -> tuple[list[EvidenceIngestInput], list[GithubTagSnapshot]]:
    names = [name for name, _sha in tags]
    existing = {
        snapshot.tag_name: snapshot
        for snapshot in GithubTagSnapshot.objects.select_for_update().filter(
            repo=repo,
            tag_name__in=names,
        )
    }
    inputs: list[EvidenceIngestInput] = []
    snapshots: list[GithubTagSnapshot] = []
    for name, sha in tags:
        previous = existing.get(name)
        revision = previous.revision if previous is not None else 0
        if previous is not None and previous.sha != sha:
            revision += 1
        snapshots.append(
            GithubTagSnapshot(
                repo=repo,
                tag_name=name,
                sha=sha,
                revision=revision,
            )
        )
        if previous is not None and previous.sha == sha:
            continue
        inputs.append(
            EvidenceIngestInput(
                source=EvidenceSource.GITHUB_STATE,
                subject=f"repo:{repo}",
                occurred_at=collected_at,
                payload={
                    "tag": safe_text(name, 100),
                    "sha": sha,
                },
                fingerprint=f"gh-tag:{_fingerprint_hash(repo, name, revision)}",
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
        )
    return inputs, snapshots


def _upsert_tag_snapshots(snapshots: list[GithubTagSnapshot]) -> None:
    if not snapshots:
        return
    GithubTagSnapshot.objects.bulk_create(
        snapshots,
        update_conflicts=True,
        update_fields=["sha", "revision", "updated_at"],
        unique_fields=["repo", "tag_name"],
    )


def _advance_ci_trains(
    *,
    repo: str,
    product: str,
    workflow_names: frozenset[str],
) -> tuple[int, bool]:
    """Recover and apply CI transitions using the deterministic workflow binding rule.

    A configured train workflow must match exactly. An unbound train advances only
    when this collection window contains runs from exactly one default-branch workflow.
    """
    advances = 0
    sole_workflow = next(iter(workflow_names)) if len(workflow_names) == 1 else None
    ambiguous = False
    if sole_workflow is None:
        ambiguous = (
            ReleaseTrain.objects.filter(
                product=product,
                phase=ReleaseTrain.Phase.PUSHED,
                head_sha__isnull=False,
            )
            .exclude(head_sha="")
            .filter(Q(ci_workflow__isnull=True) | Q(ci_workflow=""))
            .exists()
        )

    candidate_events = EvidenceEvent.objects.annotate(
        evidence_head_sha=KeyTextTransform("head_sha", "payload"),
        evidence_workflow=KeyTextTransform("workflow", "payload"),
    ).filter(
        source=EvidenceSource.CI_RUN,
        subject=f"{repo}-main-ci",
        occurred_at__gte=OuterRef("ci_epoch"),
        payload__conclusion="success",
        evidence_head_sha=OuterRef("head_sha"),
        evidence_workflow=OuterRef("recovery_workflow"),
    )
    trains = list(
        ReleaseTrain.objects.filter(
            product=product,
            phase=ReleaseTrain.Phase.PUSHED,
            head_sha__isnull=False,
        )
        .exclude(head_sha="")
        .annotate(
            ci_epoch=Greatest(
                F("phase_changed_at"),
                Coalesce(F("ci_binding_changed_at"), F("phase_changed_at")),
            ),
            recovery_workflow=Case(
                When(ci_workflow__isnull=True, then=Value(sole_workflow)),
                When(ci_workflow="", then=Value(sole_workflow)),
                default=F("ci_workflow"),
                output_field=CharField(),
            ),
        )
        .filter(recovery_workflow__isnull=False)
        .annotate(has_candidate_evidence=Exists(candidate_events))
        .filter(has_candidate_evidence=True)
        .order_by("id")[:GITHUB_RECOVERY_MAX_TRAINS]
    )
    bindings: list[tuple[ReleaseTrain, str]] = []
    for train in trains:
        if train.ci_workflow:
            workflow = train.ci_workflow
        elif sole_workflow is not None:
            workflow = sole_workflow
        else:
            continue
        bindings.append((train, workflow))

    if not bindings:
        return advances, ambiguous

    earliest_epoch = min(ci_evidence_epoch(train) for train, _workflow in bindings)
    events = list(
        EvidenceEvent.objects.filter(
            source=EvidenceSource.CI_RUN,
            subject=f"{repo}-main-ci",
            occurred_at__gte=earliest_epoch,
            payload__conclusion="success",
            payload__head_sha__in={train.head_sha for train, _workflow in bindings},
            payload__workflow__in={workflow for _train, workflow in bindings},
        )
        .order_by(
            "payload__head_sha",
            "payload__workflow",
            "-occurred_at",
            "-id",
        )
        .distinct("payload__head_sha", "payload__workflow")
    )
    latest_by_binding: dict[tuple[str, str], EvidenceEvent] = {}
    for event in events:
        key = (
            str(event.payload.get("head_sha") or ""),
            str(event.payload.get("workflow") or ""),
        )
        latest_by_binding.setdefault(key, event)

    for train, workflow in bindings:
        evidence = latest_by_binding.get((train.head_sha or "", workflow))
        if evidence is not None and evidence.occurred_at >= ci_evidence_epoch(train):
            advance_train(
                train,
                ReleaseTrain.Phase.CI_GREEN,
                evidence=evidence,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
            advances += 1
    return advances, ambiguous


@transaction.atomic
def _persist_repo(
    *,
    spec: RepoSpec,
    collection: RepoCollection,
    collected_at: datetime,
) -> tuple[int, int, str]:
    set_persistence_timeouts()
    tag_inputs, tag_snapshots = _tag_inputs(
        spec.repo,
        collection.tags,
        collected_at=collected_at,
    )
    new_inputs = _new_inputs([*collection.inputs, *tag_inputs])
    results = ingest_evidence_batch(new_inputs, now=collected_at)
    _upsert_mirrors(collection.mirrors)
    _upsert_tag_snapshots(tag_snapshots)
    advances, ambiguous = _advance_ci_trains(
        repo=spec.repo,
        product=spec.product,
        workflow_names=collection.workflow_names,
    )
    ambiguity_detail = ""
    if ambiguous:
        ambiguity_detail = f"{spec.repo}:ci_workflow_ambiguous={len(collection.workflow_names)}"
    return sum(result.created for result in results), advances, ambiguity_detail


def collect_github() -> dict[str, int]:
    """Collect GitHub state under an expiring single-run lease."""
    collected_at = timezone.now()
    held_until = acquire_collector_lease(
        CollectorStatus.Collector.GITHUB,
        now=collected_at,
    )
    if held_until is None:
        logger.info("Steward GitHub collector skipped: lease already held")
        return {"repos": 0, "pull_requests": 0, "evidence": 0, "train_advances": 0}
    try:
        return _collect_github(collected_at=collected_at)
    finally:
        release_collector_lease(
            CollectorStatus.Collector.GITHUB,
            held_until,
        )


def _collect_github(*, collected_at: datetime) -> dict[str, int]:
    """Collect GitHub state with bounded HTTP work and atomic per-repo persistence."""
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
    overall_expires_at = started_at + GITHUB_OVERALL_DEADLINE_SECONDS
    totals = {
        "repos": 0,
        "pull_requests": 0,
        "evidence": 0,
        "train_advances": 0,
    }
    failures: list[str] = []
    truncations: list[str] = []
    notices: list[str] = []
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
            for owner, repo, product in GITHUB_REPOS:
                repo_started_at = time.monotonic()
                if repo_started_at >= overall_expires_at:
                    truncations.append(f"deadline_after={totals['repos']}")
                    break
                totals["repos"] += 1
                try:
                    existing_prs = {pr.number: pr for pr in RepoPullRequest.objects.filter(repo=repo)}
                    spec = RepoSpec(owner, repo, product)
                    deadline = CollectionDeadline(
                        min(
                            overall_expires_at,
                            repo_started_at + GITHUB_REPO_DEADLINE_SECONDS,
                        )
                    )
                    collection = _collect_repo(
                        client,
                        spec,
                        collected_at=collected_at,
                        existing_prs=existing_prs,
                        deadline=deadline,
                    )
                    deadline.check()
                    evidence, advances, ambiguity_detail = _persist_repo(
                        spec=spec,
                        collection=collection,
                        collected_at=collected_at,
                    )
                    totals["pull_requests"] += len(collection.mirrors)
                    totals["evidence"] += evidence
                    totals["train_advances"] += advances
                    if ambiguity_detail:
                        notices.append(ambiguity_detail)
                    if collection.history_truncated:
                        notices.append(f"{repo}:history_truncated")
                    if collection.incomplete:
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
            detail=";".join([*notices, *failures, *truncations]),
        )
    elif truncations:
        collector_failed(
            CollectorStatus.Collector.GITHUB,
            attempted_at=collected_at,
            error_class="truncated",
            detail=";".join([*notices, *truncations]),
        )
    else:
        collector_succeeded(
            CollectorStatus.Collector.GITHUB,
            attempted_at=collected_at,
            detail=";".join([f"repos={totals['repos']}", *notices]),
        )
    return totals
