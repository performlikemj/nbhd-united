from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from apps.evals.models import EvalResult, EvalRun
from apps.evals.suites.slo_snapshot import SUITE as SLO_SUITE
from apps.evals.suites.slo_snapshot import _metric_series
from apps.steward.models import EvidenceEvent, EvidenceSource
from apps.steward.services import (
    EvidenceIngestInput,
    ingest_evidence_batch,
    stored_evidence_fingerprint,
)

_COLLECTION_LOOKBACK = timedelta(days=7)
_TERMINAL_STATUSES = (
    EvalRun.Status.PASS,
    EvalRun.Status.DEGRADED,
    EvalRun.Status.FAIL,
    EvalRun.Status.ERROR,
)


def _is_suite_transition(previous: str | None, current: str) -> bool:
    return previous is not None and previous != current


def _recent_terminal_runs(*, now) -> list[EvalRun]:
    previous = (
        EvalRun.objects.filter(
            suite=OuterRef("suite"),
            status__in=_TERMINAL_STATUSES,
            finished_at__isnull=False,
        )
        .filter(
            Q(finished_at__lt=OuterRef("finished_at"))
            | Q(
                finished_at=OuterRef("finished_at"),
                id__lt=OuterRef("id"),
            )
        )
        .order_by("-finished_at", "-id")
    )
    return list(
        EvalRun.objects.filter(
            status__in=_TERMINAL_STATUSES,
            finished_at__isnull=False,
            finished_at__gte=now - _COLLECTION_LOOKBACK,
        )
        .annotate(
            steward_previous_run_id=Subquery(previous.values("id")[:1]),
            steward_previous_status=Subquery(previous.values("status")[:1]),
        )
        .order_by("finished_at", "id")
    )


def _results_by_run(
    runs: list[EvalRun],
) -> dict[int, list[EvalResult]]:
    run_ids = {run.id for run in runs}
    run_ids.update(
        run.steward_previous_run_id
        for run in runs
        if run.suite == SLO_SUITE and run.steward_previous_run_id is not None
    )
    grouped: dict[int, list[EvalResult]] = defaultdict(list)
    for result in EvalResult.objects.filter(run_id__in=run_ids).only(
        "run_id",
        "case_id",
        "kind",
        "passed",
        "score",
        "threshold",
        "details",
    ):
        grouped[result.run_id].append(result)
    return grouped


def _suite_event_inputs(
    runs: list[EvalRun],
    results_by_run: dict[int, list[EvalResult]],
) -> list[EvidenceIngestInput]:
    inputs: list[EvidenceIngestInput] = []
    for run in runs:
        previous_status = run.steward_previous_status
        if not _is_suite_transition(previous_status, run.status):
            continue
        run_results = results_by_run.get(run.id, [])
        inputs.append(
            EvidenceIngestInput(
                source=EvidenceSource.EVAL_RUN,
                subject=f"eval:{run.suite}",
                occurred_at=run.finished_at,
                payload={
                    "run_id": run.id,
                    "status": run.status,
                    "prev_status_at_collection": previous_status,
                    "passed": sum(result.passed for result in run_results),
                    "total": len(run_results),
                    "git_sha": run.git_sha,
                },
                fingerprint=f"eval-run:{run.suite}:{run.id}",
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
        )
    return inputs


def _measured_slo_results(
    results: list[EvalResult],
) -> dict[str, EvalResult]:
    return {
        result.case_id: result
        for result in results
        if result.kind == EvalResult.Kind.SLO and result.score is not None and not result.details.get("skipped")
    }


def _slo_event_inputs(
    runs: list[EvalRun],
    results_by_run: dict[int, list[EvalResult]],
    weekly_series: dict,
) -> list[EvidenceIngestInput]:
    inputs: list[EvidenceIngestInput] = []
    for current in runs:
        previous_id = current.steward_previous_run_id
        if current.suite != SLO_SUITE or previous_id is None:
            continue
        current_results = _measured_slo_results(results_by_run.get(current.id, []))
        previous_results = _measured_slo_results(results_by_run.get(previous_id, []))

        for case_id, current_result in current_results.items():
            previous_result = previous_results.get(case_id)
            if previous_result is None or previous_result.passed == current_result.passed:
                continue
            inputs.append(
                EvidenceIngestInput(
                    source=EvidenceSource.EVAL_SLO,
                    subject=f"slo:{case_id}",
                    occurred_at=current.finished_at,
                    payload={
                        "score": str(current_result.score),
                        "threshold": (str(current_result.threshold) if current_result.threshold is not None else None),
                        "breach_days": weekly_series.get(case_id, {}).get(
                            "breach_days",
                            0,
                        ),
                    },
                    fingerprint=f"slo-transition:{case_id}:{current.id}",
                    trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                    provenance=EvidenceEvent.Provenance.COLLECTOR,
                )
            )
    return inputs


def collect_eval_evidence() -> dict[str, int]:
    """Ingest transition-relevant per-run facts and SLO breach changes."""
    collected_at = timezone.now()
    runs = _recent_terminal_runs(now=collected_at)
    results_by_run = _results_by_run(runs)
    _, _, weekly_series = _metric_series(collected_at)
    inputs = [
        *_suite_event_inputs(runs, results_by_run),
        *_slo_event_inputs(runs, results_by_run, weekly_series),
    ]
    existing_fingerprints = set(
        EvidenceEvent.objects.filter(
            fingerprint__in=[stored_evidence_fingerprint(item.source, item.fingerprint) for item in inputs]
        ).values_list("fingerprint", flat=True)
    )
    inputs = [
        item
        for item in inputs
        if stored_evidence_fingerprint(item.source, item.fingerprint) not in existing_fingerprints
    ]
    results = ingest_evidence_batch(inputs, now=collected_at)
    suite_events = sum(
        result.created for item, result in zip(inputs, results, strict=True) if item.source == EvidenceSource.EVAL_RUN
    )
    slo_events = sum(
        result.created for item, result in zip(inputs, results, strict=True) if item.source == EvidenceSource.EVAL_SLO
    )
    return {
        "eval_run": suite_events,
        "eval_slo": slo_events,
        "created": suite_events + slo_events,
    }
