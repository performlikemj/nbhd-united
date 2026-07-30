from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.evals.models import EvalResult, EvalRun
from apps.evals.suites.slo_snapshot import SUITE as SLO_SUITE
from apps.evals.suites.slo_snapshot import _metric_series
from apps.steward.models import EvidenceEvent, EvidenceSource
from apps.steward.services import ingest_evidence

_COLLECTION_LOOKBACK = timedelta(days=7)
_TERMINAL_STATUSES = (
    EvalRun.Status.PASS,
    EvalRun.Status.DEGRADED,
    EvalRun.Status.FAIL,
    EvalRun.Status.ERROR,
)
_UNHEALTHY_STATUSES = frozenset(
    {
        EvalRun.Status.DEGRADED,
        EvalRun.Status.FAIL,
        EvalRun.Status.ERROR,
    }
)


def _is_suite_transition(previous: str | None, current: str) -> bool:
    return (previous == EvalRun.Status.PASS and current in _UNHEALTHY_STATUSES) or (
        previous in _UNHEALTHY_STATUSES and current == EvalRun.Status.PASS
    )


def _previous_run(run: EvalRun) -> EvalRun | None:
    return (
        EvalRun.objects.filter(
            suite=run.suite,
            status__in=_TERMINAL_STATUSES,
            finished_at__isnull=False,
        )
        .filter(Q(finished_at__lt=run.finished_at) | Q(finished_at=run.finished_at, id__lt=run.id))
        .order_by("-finished_at", "-id")
        .first()
    )


def _recent_terminal_runs(*, suite: str | None = None) -> list[EvalRun]:
    runs = EvalRun.objects.filter(
        status__in=_TERMINAL_STATUSES,
        finished_at__isnull=False,
        finished_at__gte=timezone.now() - _COLLECTION_LOOKBACK,
    )
    if suite is not None:
        runs = runs.filter(suite=suite)
    return list(runs.order_by("finished_at", "id"))


def _collect_suite_transitions() -> int:
    created_count = 0
    runs = _recent_terminal_runs()
    previous_by_suite: dict[str, str | None] = {}
    for run in runs:
        if run.suite not in previous_by_suite:
            previous = _previous_run(run)
            previous_by_suite[run.suite] = previous.status if previous else None
        previous_status = previous_by_suite[run.suite]
        if _is_suite_transition(previous_status, run.status):
            total = run.results.count()
            passed = run.results.filter(passed=True).count()
            result = ingest_evidence(
                source=EvidenceSource.EVAL_RUN,
                subject=f"eval:{run.suite}",
                occurred_at=run.finished_at,
                payload={
                    "status": run.status,
                    "prev_status": previous_status,
                    "passed": passed,
                    "total": total,
                    "git_sha": run.git_sha,
                },
                fingerprint=f"eval-transition:{run.suite}:{run.id}",
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
            created_count += result.created
        previous_by_suite[run.suite] = run.status
    return created_count


def _measured_slo_results(run: EvalRun) -> dict[str, EvalResult]:
    return {
        result.case_id: result
        for result in run.results.filter(
            kind=EvalResult.Kind.SLO,
            score__isnull=False,
        )
        if not result.details.get("skipped")
    }


def _collect_slo_transitions() -> int:
    created_count = 0
    for current in _recent_terminal_runs(suite=SLO_SUITE):
        previous = _previous_run(current)
        if previous is None:
            continue
        current_results = _measured_slo_results(current)
        previous_results = _measured_slo_results(previous)
        _, _, weekly_series = _metric_series(current.finished_at)

        for case_id, current_result in current_results.items():
            previous_result = previous_results.get(case_id)
            if previous_result is None or previous_result.passed == current_result.passed:
                continue
            result = ingest_evidence(
                source=EvidenceSource.EVAL_SLO,
                subject=f"slo:{case_id}",
                occurred_at=current.finished_at,
                payload={
                    "score": (str(current_result.score) if current_result.score is not None else None),
                    "threshold": (str(current_result.threshold) if current_result.threshold is not None else None),
                    "breach_days": weekly_series.get(case_id, {}).get("breach_days", 0),
                },
                fingerprint=f"slo-transition:{case_id}:{current.id}",
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )
            created_count += result.created
    return created_count


def collect_eval_evidence() -> dict[str, int]:
    """Ingest metadata-only suite and SLO breach-state transitions."""
    suite_events = _collect_suite_transitions()
    slo_events = _collect_slo_transitions()
    return {
        "eval_run": suite_events,
        "eval_slo": slo_events,
        "created": suite_events + slo_events,
    }
