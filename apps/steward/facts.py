from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from django.db.models import Count, Q

from apps.evals.models import EvalResult, EvalRun
from apps.evals.suites.slo_snapshot import SUITE as SLO_SUITE
from apps.evals.suites.slo_snapshot import _metric_series
from apps.steward.collectors.openrouter import NULL_RATE_THRESHOLD_PCT
from apps.steward.models import (
    AlertState,
    CollectorStatus,
    EvidenceEvent,
    EvidenceSource,
    Expectation,
    ReleaseTrain,
    RepoPullRequest,
    TrackedItem,
)
from apps.steward.sanitize import safe_text
from apps.steward.trains import (
    TERMINAL_PHASES,
    TRAIN_EXPECTATION_OWNER,
    next_phase_for,
    train_subject,
)

_SWEEP_LIVENESS_FINGERPRINT = "steward-sweep:liveness"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _age_seconds(now: datetime, then: datetime | None) -> int | None:
    if then is None:
        return None
    return max(0, int((now - then).total_seconds()))


def _age_days(now: datetime, then: datetime) -> int:
    return max(0, (now - then).days)


def _next_nag_days(age_days: int) -> int:
    if age_days < 2:
        return 2 - age_days
    if age_days < 5:
        return 5 - age_days
    if age_days < 10:
        return 10 - age_days
    next_day = 10 + (math.floor((age_days - 10) / 7) + 1) * 7
    return next_day - age_days


def _nag_today(age_days: int) -> bool:
    return age_days in {2, 5, 10} or (age_days > 10 and (age_days - 10) % 7 == 0)


def _link_from_refs(refs: object) -> str | None:
    if not isinstance(refs, list):
        return None
    for ref in refs:
        if (
            isinstance(ref, dict)
            and ref.get("type") == "url"
            and isinstance(ref.get("value"), str)
            and ref["value"].startswith(("https://", "http://"))
        ):
            return ref["value"]
    return None


def _needs_you(now: datetime) -> list[dict[str, Any]]:
    facts = []
    items = TrackedItem.objects.filter(
        Q(status=TrackedItem.Status.BLOCKED)
        | Q(
            kind=TrackedItem.Kind.BLOCKED_ON_MJ,
            status=TrackedItem.Status.ACTIVE,
        )
    ).order_by("status_changed_at", "id")
    for item in items:
        waiting_days = _age_days(now, item.status_changed_at)
        facts.append(
            {
                "id": f"tracked-item:{item.pk}",
                "tracked_item_id": item.pk,
                "title": safe_text(item.title, 200),
                "context": safe_text(item.context, 240),
                "status": item.status,
                "status_changed_at": _iso(item.status_changed_at),
                "waiting_seconds": _age_seconds(now, item.status_changed_at),
                "waiting_days": waiting_days,
                "remind_today": _nag_today(waiting_days),
                "next_reminder_days": _next_nag_days(waiting_days),
                "hint": safe_text(item.context, 240),
                "link": _link_from_refs(item.refs),
                "already_alerted": False,
            }
        )
    return facts


def _effective_due(expectation: Expectation) -> datetime | None:
    if expectation.kind == Expectation.Kind.DEADLINE:
        return expectation.due_at
    if expectation.last_satisfied_at is None or not expectation.interval_s:
        return None
    return expectation.last_satisfied_at + timedelta(seconds=expectation.interval_s)


def _stalled(now: datetime) -> list[dict[str, Any]]:
    expectations = list(Expectation.objects.filter(state=Expectation.State.MISSED).order_by("id"))
    fingerprints = {
        expectation.pk: f"steward-miss:{expectation.pk}:{expectation.miss_count}"
        for expectation in expectations
        if expectation.on_miss == Expectation.OnMiss.URGENT
    }
    sent_fingerprints = set(
        AlertState.objects.filter(
            fingerprint__in=fingerprints.values(),
            last_sent_at__isnull=False,
        ).values_list("fingerprint", flat=True)
    )
    facts = []
    for expectation in expectations:
        due_at = _effective_due(expectation)
        alert_fingerprint = fingerprints.get(expectation.pk)
        facts.append(
            {
                "id": f"expectation:{expectation.pk}",
                "expectation_id": expectation.pk,
                "subject": safe_text(expectation.subject, 128),
                "kind": expectation.kind,
                "state": expectation.state,
                "on_miss": expectation.on_miss,
                "due_at": _iso(due_at),
                "overdue_seconds": _age_seconds(now, due_at),
                "last_alerted_at": _iso(expectation.last_alerted_at),
                "alert_age_seconds": _age_seconds(now, expectation.last_alerted_at),
                "miss_count": expectation.miss_count,
                "hint": "close, re-date, or restore evidence",
                "link": _link_from_refs(expectation.subject_item.refs) if expectation.subject_item_id else None,
                "already_alerted": bool(alert_fingerprint and alert_fingerprint in sent_fingerprints),
            }
        )
    return facts


def _trains(now: datetime) -> list[dict[str, Any]]:
    today = now.date()
    trains = ReleaseTrain.objects.filter(
        Q(phase__in=TERMINAL_PHASES, phase_changed_at__date=today) | ~Q(phase__in=TERMINAL_PHASES)
    ).order_by("product", "version_string", "id")
    facts = []
    for train in trains:
        expectation = None
        next_phase = None
        due_at = None
        if train.phase not in TERMINAL_PHASES:
            expectation = (
                Expectation.objects.filter(
                    subject=train_subject(train),
                    owner=TRAIN_EXPECTATION_OWNER,
                    state=Expectation.State.ARMED,
                )
                .order_by("-due_at", "-id")
                .first()
            )
            next_phase = next_phase_for(train) or "unknown"
            due_at = expectation.due_at if expectation else None
        facts.append(
            {
                "id": f"train:{train.pk}",
                "train_id": train.pk,
                "product": train.product,
                "version_string": safe_text(train.version_string, 32),
                "phase": train.phase,
                "phase_changed_at": _iso(train.phase_changed_at),
                "phase_age_seconds": _age_seconds(now, train.phase_changed_at),
                "next_phase": next_phase,
                "due_at": _iso(due_at),
                "hint": (
                    f"next: {next_phase} due {due_at.strftime('%Y-%m-%d') if due_at else 'unknown'}"
                    if next_phase
                    else ""
                ),
                "link": _link_from_refs(train.refs),
                "already_alerted": False,
            }
        )
    return facts


def _failing_evals(now: datetime) -> list[dict[str, Any]]:
    runs = (
        EvalRun.objects.filter(
            status__in=[
                EvalRun.Status.PASS,
                EvalRun.Status.DEGRADED,
                EvalRun.Status.FAIL,
                EvalRun.Status.ERROR,
            ],
            finished_at__isnull=False,
        )
        .order_by("suite", "-finished_at", "-id")
        .distinct("suite")
    )
    facts = []
    for run in runs:
        if run.status not in {EvalRun.Status.FAIL, EvalRun.Status.ERROR}:
            continue
        facts.append(
            {
                "id": f"eval-run:{run.pk}",
                "run_id": run.pk,
                "suite": safe_text(run.suite, 200),
                "status": run.status,
                "finished_at": _iso(run.finished_at),
                "age_seconds": _age_seconds(now, run.finished_at),
                "hint": "open the run, fix, or park",
                "link": None,
                "already_alerted": False,
            }
        )
    return facts


def _slo_breaches(now: datetime) -> list[dict[str, Any]]:
    latest = EvalRun.objects.filter(suite=SLO_SUITE, finished_at__isnull=False).order_by("-finished_at", "-id").first()
    if latest is None:
        return []
    _, _, series = _metric_series(now)
    facts = []
    for result in latest.results.filter(
        kind=EvalResult.Kind.SLO,
        passed=False,
        score__isnull=False,
    ).order_by("case_id"):
        facts.append(
            {
                "id": f"slo:{latest.pk}:{result.case_id}",
                "run_id": latest.pk,
                "case_id": safe_text(result.case_id, 200),
                "score": str(result.score),
                "threshold": str(result.threshold),
                "breach_days": series.get(result.case_id, {}).get("breach_days", 0),
                "finished_at": _iso(latest.finished_at),
                "age_seconds": _age_seconds(now, latest.finished_at),
                "hint": "inspect latest slo_snapshot run",
                "link": None,
                "already_alerted": False,
            }
        )
    return facts


def _openrouter_severe(since: datetime) -> list[dict[str, Any]]:
    events = (
        EvidenceEvent.objects.filter(
            received_at__gt=since,
            source=EvidenceSource.OPENROUTER_MODEL_HEALTH,
        )
        .exclude(trust=EvidenceEvent.Trust.UNTRUSTED_TEXT)
        .order_by("received_at", "id")
    )
    facts = []
    for event in events:
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        kind = payload.get("kind")
        scope = payload.get("scope")
        baseline_days = payload.get("baseline_days")
        severe = payload.get("severe")
        if (
            scope not in {"account", "canary", "provider"}
            or not isinstance(baseline_days, int)
            or isinstance(baseline_days, bool)
            or baseline_days < 3
            or severe is not True
        ):
            continue
        model = payload.get("model")
        if not isinstance(model, str) or not model:
            continue
        fact = {
            "id": f"evidence:{event.fingerprint}",
            "evidence_fingerprint": event.fingerprint,
            "kind": kind,
            "scope": scope,
            "model": safe_text(model, 200),
            "baseline_days": baseline_days,
            "occurred_at": _iso(event.occurred_at),
            "received_at": _iso(event.received_at),
            "link": None,
            "already_alerted": False,
        }
        current_pct = payload.get("current_pct")
        if kind == "null_rate":
            if (
                isinstance(current_pct, bool)
                or not isinstance(current_pct, (int, float))
                or not math.isfinite(current_pct)
            ):
                continue
            fact.update(
                {
                    "current_pct": current_pct,
                    "threshold_pct": NULL_RATE_THRESHOLD_PCT,
                    "hint": "switch model/provider route or set a fallback",
                }
            )
        elif kind == "tool_calls_share_drop":
            baseline_pct = payload.get("baseline_pct")
            drop_pts = payload.get("drop_pts")
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in (current_pct, baseline_pct, drop_pts)
            ):
                continue
            fact.update(
                {
                    "current_pct": current_pct,
                    "baseline_pct": baseline_pct,
                    "drop_pts": drop_pts,
                    "hint": "model stopped using tools; check the model/provider change",
                }
            )
        else:
            continue
        facts.append(fact)
    return facts


def _stale_prs(now: datetime) -> list[dict[str, Any]]:
    stale_before = now - timedelta(days=7)
    facts = []
    pull_requests = RepoPullRequest.objects.filter(
        state=RepoPullRequest.State.OPEN,
        is_dependabot=False,
        last_activity_at__lt=stale_before,
    ).order_by("repo", "last_activity_at", "number")
    for pull_request in pull_requests:
        facts.append(
            {
                "id": f"pr:{pull_request.repo}:{pull_request.number}",
                "repo": pull_request.repo,
                "number": pull_request.number,
                "title": safe_text(pull_request.title, 140),
                "author": safe_text(pull_request.author, 60),
                "draft": pull_request.draft,
                "status": pull_request.state,
                "last_activity_at": _iso(pull_request.last_activity_at),
                "quiet_seconds": _age_seconds(now, pull_request.last_activity_at),
                "hint": "review, rebase, or close",
                "link": f"https://github.com/performlikemj/{pull_request.repo}/pull/{pull_request.number}",
                "already_alerted": False,
            }
        )
    return facts


def _integrity(now: datetime) -> list[dict[str, Any]]:
    facts = []
    items = TrackedItem.objects.filter(status__in=[TrackedItem.Status.ACTIVE, TrackedItem.Status.PARKED]).annotate(
        armed_expectations=Count(
            "expectations",
            filter=Q(expectations__state=Expectation.State.ARMED),
            distinct=True,
        )
    )
    for item in items.filter(armed_expectations=0).order_by("product", "title", "id"):
        issue = (
            "active with zero armed expectations"
            if item.status == TrackedItem.Status.ACTIVE
            else "parked with no revisit expectation"
        )
        facts.append(
            {
                "id": f"tracked-item:{item.pk}",
                "tracked_item_id": item.pk,
                "title": safe_text(item.title, 200),
                "product": item.product,
                "status": item.status,
                "issue": issue,
                "hint": issue,
                "link": _link_from_refs(item.refs),
                "already_alerted": False,
            }
        )

    intervals = {
        CollectorStatus.Collector.GITHUB: timedelta(minutes=30),
        CollectorStatus.Collector.ASC: timedelta(hours=1),
        CollectorStatus.Collector.OPENROUTER: timedelta(days=1),
    }
    statuses = {status.collector: status for status in CollectorStatus.objects.filter(collector__in=intervals)}
    for collector, interval in intervals.items():
        status = statuses.get(collector)
        issue = None
        if status is None or status.last_success_at is None:
            issue = "never succeeded"
        if status is not None and status.last_error_class == "not_configured":
            issue = "not_configured"
        elif status is not None and status.last_success_at is not None:
            if status.last_success_at <= now - 3 * interval:
                age_seconds = _age_seconds(now, status.last_success_at)
                issue = f"stale; last success {_age_label(age_seconds)}"
            elif status.consecutive_failures:
                issue = f"{status.last_error_class or 'failed'} ({status.consecutive_failures} consecutive)"
        if issue is None:
            continue
        facts.append(
            {
                "id": f"collector:{collector}",
                "collector": collector,
                "status": status.last_error_class if status else "missing",
                "last_success_at": _iso(status.last_success_at) if status else None,
                "last_success_age_seconds": _age_seconds(now, status.last_success_at) if status else None,
                "last_attempt_at": _iso(status.last_attempt_at) if status else None,
                "consecutive_failures": status.consecutive_failures if status else 0,
                "issue": issue,
                "hint": issue,
                "link": None,
                "already_alerted": False,
            }
        )
    return facts


def _age_label(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "unknown"
    if age_seconds < 3600:
        return f"{age_seconds // 60}m ago"
    if age_seconds < 86400:
        return f"{age_seconds // 3600}h ago"
    return f"{age_seconds // 86400}d ago"


def compose_steward_facts(now: datetime, since: datetime) -> dict[str, Any]:
    """Compose the deterministic, JSON-safe Steward facts snapshot."""
    now = now.astimezone(UTC)
    since = since.astimezone(UTC)
    needs_you = _needs_you(now)
    stalled = _stalled(now)
    trains = _trains(now)
    failing_evals = _failing_evals(now)
    slo_breaches = _slo_breaches(now)
    openrouter_severe = _openrouter_severe(since)
    stale_prs = _stale_prs(now)
    integrity = _integrity(now)
    sweep_state = AlertState.objects.filter(fingerprint=_SWEEP_LIVENESS_FINGERPRINT).first()

    return {
        "version": 1,
        "generated_at": _iso(now),
        "since": _iso(since),
        "stats": {
            "needs_you": len(needs_you),
            "trains": len(trains),
            "stalled": len(stalled),
            "slo_evals": len(failing_evals) + len(slo_breaches),
            "openrouter": len(openrouter_severe),
            "repos": len(stale_prs),
            "integrity": len(integrity),
        },
        "stalled": stalled,
        "slo_breaches": slo_breaches,
        "failing_evals": failing_evals,
        "openrouter_severe": openrouter_severe,
        "stale_prs": stale_prs,
        "integrity": integrity,
        "needs_you": needs_you,
        "trains": trains,
        "liveness": {
            "armed_expectations": Expectation.objects.filter(state=Expectation.State.ARMED).count(),
            "last_sweep_at": _iso(sweep_state.last_sent_at) if sweep_state else None,
            "last_sweep_age_seconds": (_age_seconds(now, sweep_state.last_sent_at) if sweep_state else None),
        },
    }
