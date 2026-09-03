from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.evals.models import EvalResult, EvalRun
from apps.evals.suites.slo_snapshot import SUITE as SLO_SUITE
from apps.evals.suites.slo_snapshot import _metric_series
from apps.steward.collectors.evals import collect_eval_evidence
from apps.steward.collectors.openrouter import NULL_RATE_THRESHOLD_PCT
from apps.steward.models import (
    AlertState,
    CollectorStatus,
    DigestRecord,
    EvidenceEvent,
    EvidenceSource,
    Expectation,
    ReleaseTrain,
    RepoPullRequest,
    TrackedItem,
)
from apps.steward.notify import send_digest
from apps.steward.sanitize import safe_text as _safe_text
from apps.steward.trains import (
    TERMINAL_PHASES,
    TRAIN_EXPECTATION_OWNER,
    next_phase_for,
    train_subject,
)

logger = logging.getLogger(__name__)

MAX_DIGEST_CHARS = 3500
MAX_SECTION_LINES = 10
MAX_RENDERED_SUBJECT_CHARS = 80
CLOSING_HINT = "Reply on Telegram or run: python manage.py steward_ack <expectation_id> / steward_decide"
_SWEEP_LIVENESS_FINGERPRINT = "steward-sweep:liveness"


def _age_days(now: datetime, then: datetime) -> int:
    return max(0, (now - then).days)


def _age_label(now: datetime, then: datetime | None) -> str:
    if then is None:
        return "unknown"
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


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


def _needs_you(now: datetime) -> tuple[list[str], int]:
    items = list(
        TrackedItem.objects.filter(
            Q(status=TrackedItem.Status.BLOCKED)
            | Q(
                kind=TrackedItem.Kind.BLOCKED_ON_MJ,
                status=TrackedItem.Status.ACTIVE,
            )
        ).order_by("status_changed_at", "id")
    )
    reminders: list[str] = []
    waiting: list[tuple[TrackedItem, int]] = []
    for item in items:
        age = _age_days(now, item.status_changed_at)
        if _nag_today(age):
            context = _safe_text(item.context, 240)
            detail = f" — {context}" if context else ""
            reminders.append(f"- {_safe_text(item.title, 200)} — {age}d waiting{detail}")
        else:
            waiting.append((item, age))
    if waiting:
        oldest_age = max(age for _, age in waiting)
        reminders.append(
            f"- {len(waiting)} items waiting (next reminder for oldest in {_next_nag_days(oldest_age)} days)"
        )
    return reminders, len(items)


def _effective_due(expectation: Expectation) -> datetime | None:
    if expectation.kind == Expectation.Kind.DEADLINE:
        return expectation.due_at
    if expectation.last_satisfied_at is None or not expectation.interval_s:
        return None
    return expectation.last_satisfied_at + timedelta(seconds=expectation.interval_s)


def _stalled(now: datetime) -> tuple[list[str], int]:
    expectations = list(Expectation.objects.filter(state=Expectation.State.MISSED).order_by("id"))
    lines: list[str] = []
    for expectation in expectations:
        due = _effective_due(expectation)
        overdue = _age_label(now, due).removesuffix(" ago")
        alerted = ""
        if expectation.on_miss == Expectation.OnMiss.URGENT and expectation.last_alerted_at is not None:
            alerted = f"; alerted {_age_label(now, expectation.last_alerted_at)}"
        lines.append(
            f"- {_safe_text(expectation.subject, MAX_RENDERED_SUBJECT_CHARS)} — "
            f"{overdue} overdue{alerted} — close, re-date, or restore evidence"
        )
    return lines, len(expectations)


def _trains(now: datetime) -> tuple[list[str], int]:
    today = now.astimezone(UTC).date()
    trains = list(
        ReleaseTrain.objects.filter(
            Q(phase__in=TERMINAL_PHASES, phase_changed_at__date=today) | ~Q(phase__in=TERMINAL_PHASES)
        ).order_by("product", "version_string", "id")
    )
    lines: list[str] = []
    for train in trains:
        age = _age_days(now, train.phase_changed_at)
        if train.phase in TERMINAL_PHASES:
            lines.append(f"- {train.product} {train.version_string}: {train.phase} ({age}d)")
            continue
        expectation = (
            Expectation.objects.filter(
                subject=train_subject(train),
                owner=TRAIN_EXPECTATION_OWNER,
                state=Expectation.State.ARMED,
            )
            .order_by("-due_at", "-id")
            .first()
        )
        upcoming = next_phase_for(train)
        next_phase = upcoming if upcoming is not None else "unknown"
        due = expectation.due_at.strftime("%Y-%m-%d") if expectation and expectation.due_at else "unknown"
        lines.append(f"- {train.product} {train.version_string}: {train.phase} ({age}d) — next: {next_phase} due {due}")
    return lines, len(trains)


def _latest_unhealthy_suites(now: datetime) -> list[str]:
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
    lines: list[str] = []
    for run in runs:
        if run.status == EvalRun.Status.FAIL:
            state = "failing"
        elif run.status == EvalRun.Status.ERROR:
            state = "errored"
        else:
            continue
        lines.append(
            f"- EVAL {_safe_text(run.suite, MAX_RENDERED_SUBJECT_CHARS)}: "
            f"{state} since {_age_label(now, run.finished_at)} (run {run.id}) "
            "— open the run, fix, or park"
        )
    return lines


def _latest_slo_breaches(now: datetime) -> list[str]:
    latest = (
        EvalRun.objects.filter(
            suite=SLO_SUITE,
            finished_at__isnull=False,
        )
        .order_by("-finished_at", "-id")
        .first()
    )
    if latest is None:
        return []
    _, _, series = _metric_series(now)
    lines = []
    for result in latest.results.filter(
        kind=EvalResult.Kind.SLO,
        passed=False,
        score__isnull=False,
    ).order_by("case_id"):
        breach_days = series.get(result.case_id, {}).get("breach_days", 0)
        lines.append(
            f"- SLO {_safe_text(result.case_id, MAX_RENDERED_SUBJECT_CHARS)}: "
            f"{result.score} vs {result.threshold} ({breach_days} breach days) "
            "— inspect latest slo_snapshot run"
        )
    return lines


def _trusted_changes_since(since: datetime):
    return EvidenceEvent.objects.filter(
        received_at__gt=since,
    ).exclude(trust=EvidenceEvent.Trust.UNTRUSTED_TEXT)


def _slo_and_evals(now: datetime) -> tuple[list[str], int]:
    lines = [*_latest_unhealthy_suites(now), *_latest_slo_breaches(now)]
    return lines, len(lines)


def _openrouter_health(since: datetime) -> tuple[list[str], int]:
    lines: list[str] = []
    events = (
        _trusted_changes_since(since)
        .filter(source=EvidenceSource.OPENROUTER_MODEL_HEALTH)
        .order_by("received_at", "id")
    )
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
            or not isinstance(severe, bool)
        ):
            continue
        if severe and kind == "null_rate":
            model = payload.get("model")
            current_pct = payload.get("current_pct")
            if (
                not isinstance(model, str)
                or not model
                or isinstance(current_pct, bool)
                or not isinstance(current_pct, (int, float))
                or not math.isfinite(current_pct)
            ):
                continue
            lines.append(
                f"- {scope} {_safe_text(model, MAX_RENDERED_SUBJECT_CHARS)}: "
                f"null finish_reason {current_pct:.2f}% "
                f"(> {NULL_RATE_THRESHOLD_PCT:.2f}%) "
                "— switch model/provider route or set a fallback"
            )
        elif severe and kind == "tool_calls_share_drop":
            model = payload.get("model")
            current_pct = payload.get("current_pct")
            baseline_pct = payload.get("baseline_pct")
            drop_pts = payload.get("drop_pts")
            if (
                not isinstance(model, str)
                or not model
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    for value in (current_pct, baseline_pct, drop_pts)
                )
            ):
                continue
            lines.append(
                f"- {scope} {_safe_text(model, MAX_RENDERED_SUBJECT_CHARS)}: "
                f"tool_calls {current_pct:.2f}% vs {baseline_pct:.2f}% "
                f"({drop_pts:.2f} pts drop) "
                "— model stopped using tools; check the model/provider change"
            )
    return lines, len(lines)


def _repos(now: datetime) -> tuple[list[str], int]:
    stale_before = now - timedelta(days=7)
    lines: list[str] = []
    stale_human_pull_requests = RepoPullRequest.objects.filter(
        state=RepoPullRequest.State.OPEN,
        is_dependabot=False,
        last_activity_at__lt=stale_before,
    ).order_by("repo", "last_activity_at", "number")
    for pull_request in stale_human_pull_requests:
        quiet_days = _age_days(now, pull_request.last_activity_at)
        lines.append(
            f"- {pull_request.repo} #{pull_request.number} — "
            f"{_safe_text(pull_request.title, 60)} — {quiet_days}d quiet "
            "— review, rebase, or close"
        )
    return lines, len(lines)


def _integrity(now: datetime) -> tuple[list[str], int]:
    items = TrackedItem.objects.filter(status__in=[TrackedItem.Status.ACTIVE, TrackedItem.Status.PARKED]).annotate(
        armed_expectations=Count(
            "expectations",
            filter=Q(expectations__state=Expectation.State.ARMED),
            distinct=True,
        )
    )
    lines = []
    for item in items.filter(armed_expectations=0).order_by("product", "title", "id"):
        if item.status == TrackedItem.Status.ACTIVE:
            issue = "active with zero armed expectations"
        else:
            issue = "parked with no revisit expectation"
        lines.append(f"- {_safe_text(item.title, 200)} — {issue}")
    intervals = {
        CollectorStatus.Collector.GITHUB: timedelta(minutes=30),
        CollectorStatus.Collector.ASC: timedelta(hours=1),
        CollectorStatus.Collector.OPENROUTER: timedelta(days=1),
    }
    statuses = {status.collector: status for status in CollectorStatus.objects.filter(collector__in=intervals)}
    for collector, interval in intervals.items():
        status = statuses.get(collector)
        if status is None:
            lines.append(f"- collector {collector}: never succeeded")
            continue
        if status.last_error_class == "not_configured":
            lines.append(f"- collector {collector}: not_configured")
            continue
        if status.last_success_at is None:
            lines.append(f"- collector {collector}: never succeeded")
            continue
        if status.last_success_at <= now - 3 * interval:
            lines.append(f"- collector {collector}: stale; last success {_age_label(now, status.last_success_at)}")
            continue
        if status.consecutive_failures:
            lines.append(
                f"- collector {collector}: {status.last_error_class or 'failed'} "
                f"({status.consecutive_failures} consecutive)"
            )
    return lines, len(lines)


def _omission_marker(omitted: int) -> str:
    return f"… +{omitted} lines omitted"


def _minimum_section_block(title: str, lines: list[str], count: int) -> str:
    return f"\n\n{title} ({count})\n{lines[0]}"


def _render_section_block(
    title: str,
    lines: list[str],
    count: int,
    *,
    budget: int,
) -> str:
    heading = f"\n\n{title} ({count})"
    limited_lines = lines[:MAX_SECTION_LINES]
    all_lines = f"{heading}\n" + "\n".join(limited_lines)
    if len(lines) <= MAX_SECTION_LINES and len(all_lines) <= budget:
        return all_lines

    kept: list[str] = [limited_lines[0]]
    for line in limited_lines[1:]:
        proposed = [*kept, line]
        omitted = len(lines) - len(proposed)
        candidate = f"{heading}\n" + "\n".join(proposed) + f"\n{_omission_marker(omitted)}"
        if len(candidate) > budget:
            break
        kept = proposed

    omitted = len(lines) - len(kept)
    detail_lines = list(kept)
    marker = _omission_marker(omitted)
    candidate = f"{heading}\n" + "\n".join([*detail_lines, marker])
    if omitted and len(candidate) <= budget:
        detail_lines.append(marker)
    detail = "\n".join(detail_lines)
    return f"{heading}\n{detail}"


def _render_budgeted_sections(
    header: str,
    sections: list[tuple[str, list[str], int]],
    *,
    footer: str = "",
) -> str:
    rendered = header
    footer_block = f"\n\n{footer}" if footer else ""
    for index, (title, lines, count) in enumerate(sections):
        remaining_minimum = sum(len(_minimum_section_block(*section)) for section in sections[index + 1 :])
        section_budget = MAX_DIGEST_CHARS - len(rendered) - remaining_minimum - len(footer_block)
        rendered += _render_section_block(
            title,
            lines,
            count,
            budget=section_budget,
        )
    return rendered + footer_block


def render_steward_daily_digest(
    *,
    now: datetime | None = None,
) -> tuple[str, dict[str, int]]:
    now = now or timezone.now()
    last_delivered = (
        DigestRecord.objects.filter(delivery=DigestRecord.Delivery.DELIVERED).order_by("-sent_at", "-id").first()
    )
    since = last_delivered.sent_at if last_delivered else now - timedelta(hours=24)
    slo_evals, slo_evals_count = _slo_and_evals(now)
    needs_you = _needs_you(now)
    trains = _trains(now)
    stalled = _stalled(now)
    repos = _repos(now)
    openrouter_health = _openrouter_health(since)
    integrity = _integrity(now)

    sections = [
        ("NEEDS YOU", *needs_you),
        ("STALLED", *stalled),
        ("TRAINS", *trains),
        ("SLO / EVALS", slo_evals, slo_evals_count),
        ("OPENROUTER", *openrouter_health),
        ("REPOS", *repos),
        ("INTEGRITY", *integrity),
    ]
    stats = {
        "needs_you": needs_you[1],
        "trains": trains[1],
        "stalled": stalled[1],
        "slo_evals": slo_evals_count,
        "openrouter": openrouter_health[1],
        "repos": repos[1],
        "integrity": integrity[1],
    }

    header = "\n".join(["STEWARD DAILY FACTS", now.strftime("%Y-%m-%d UTC")])
    nonempty_sections = [section for section in sections if section[1]]
    if nonempty_sections:
        rendered = _render_budgeted_sections(
            header,
            nonempty_sections,
            footer=CLOSING_HINT,
        )
    else:
        armed = Expectation.objects.filter(state=Expectation.State.ARMED).count()
        sweep_state = AlertState.objects.filter(fingerprint=_SWEEP_LIVENESS_FINGERPRINT).first()
        sweep_age = _age_label(
            now,
            sweep_state.last_sent_at if sweep_state else None,
        )
        rendered = "\n".join(
            [
                header,
                "",
                "ALL QUIET",
                f"All quiet — {armed} expectations armed, last sweep {sweep_age}.",
            ]
        )
    return rendered, stats


def run_steward_daily_digest() -> dict[str, object]:
    """Collect fresh eval facts, render, deliver, and record the daily digest."""
    rendered_at = timezone.now()
    period_date = rendered_at.astimezone(UTC).date()
    collect_eval_evidence()
    text, stats = render_steward_daily_digest(now=rendered_at)

    with transaction.atomic():
        record, _ = DigestRecord.objects.get_or_create(
            period_date=period_date,
            defaults={
                "sent_at": rendered_at,
                "delivery": DigestRecord.Delivery.TRANSIENT,
                "body": "",
                "stats": {},
            },
        )

    with transaction.atomic():
        record = DigestRecord.objects.select_for_update().get(pk=record.pk)
        if record.delivery == DigestRecord.Delivery.DELIVERED or record.body:
            return {
                "delivery": record.delivery,
                "digest_id": record.id,
                "chars": len(record.body),
                "stats": record.stats,
                "skipped": True,
            }

        try:
            delivery = send_digest(text)
        except Exception as exc:
            logger.error(
                "Steward digest notifier raised error_class=%s",
                type(exc).__name__,
            )
            delivery = DigestRecord.Delivery.TRANSIENT
        if delivery not in DigestRecord.Delivery.values:
            delivery = DigestRecord.Delivery.TRANSIENT
        record.sent_at = timezone.now()
        record.delivery = delivery
        record.body = text
        record.stats = stats
        record.full_clean()
        record.save(update_fields=["sent_at", "delivery", "body", "stats"])
    return {
        "delivery": delivery,
        "digest_id": record.id,
        "chars": len(text),
        "stats": stats,
        "skipped": False,
    }
