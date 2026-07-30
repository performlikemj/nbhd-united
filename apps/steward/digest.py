from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import datetime, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from apps.evals.models import EvalResult, EvalRun
from apps.evals.suites.slo_snapshot import SUITE as SLO_SUITE
from apps.evals.suites.slo_snapshot import _metric_series
from apps.steward.collectors.evals import collect_eval_evidence
from apps.steward.models import (
    AlertState,
    DigestRecord,
    EvidenceEvent,
    EvidenceSource,
    Expectation,
    TrackedItem,
)
from apps.steward.notify import send_digest

logger = logging.getLogger(__name__)

MAX_DIGEST_CHARS = 3500
MAX_SECTION_LINES = 10
_SWEEP_LIVENESS_FINGERPRINT = "steward-sweep:liveness"


def _safe_text(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"


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


def _cap_section(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_SECTION_LINES:
        return lines
    kept = lines[:MAX_SECTION_LINES]
    kept.append(f"… +{len(lines) - MAX_SECTION_LINES} entries omitted")
    return kept


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
    return _cap_section(reminders), len(items)


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
        lines.append(f"- {_safe_text(expectation.subject, 128)} — {overdue} overdue{alerted}")
    return _cap_section(lines), len(expectations)


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
            f"- SLO {result.case_id}: score {result.score} vs threshold {result.threshold}; breach_days {breach_days}"
        )
    return lines


def _trusted_changes_since(since: datetime):
    return EvidenceEvent.objects.filter(
        occurred_at__gt=since,
    ).exclude(trust=EvidenceEvent.Trust.UNTRUSTED_TEXT)


def _validated_eval_transition(event: EvidenceEvent) -> tuple[str, str] | None:
    if event.source != EvidenceSource.EVAL_RUN or not event.subject.startswith("eval:"):
        return None
    status = event.payload.get("status")
    previous = event.payload.get("prev_status")
    if status not in EvalRun.Status.values or previous not in EvalRun.Status.values:
        return None
    return previous, status


def _slo_and_evals(now: datetime, since: datetime) -> tuple[list[str], int]:
    lines = _latest_slo_breaches(now)
    for event in _trusted_changes_since(since).filter(source=EvidenceSource.EVAL_RUN).order_by("occurred_at", "id"):
        transition = _validated_eval_transition(event)
        if transition is None:
            continue
        previous, status = transition
        suite = _safe_text(event.subject.removeprefix("eval:"), 64)
        lines.append(f"- EVAL {suite}: {previous} -> {status}")
    return _cap_section(lines), len(lines)


def _changes(since: datetime) -> tuple[list[str], int]:
    events = list(_trusted_changes_since(since).order_by("occurred_at", "id"))
    counts = Counter(event.source for event in events)
    lines = [f"- {source}: {counts[source]}" for source in sorted(counts)]

    for source in sorted(counts):
        if not EvidenceEvent.objects.filter(
            source=source,
            occurred_at__lte=since,
        ).exists():
            lines.append(f"- new source: {source}")

    for event in events:
        transition = _validated_eval_transition(event)
        if transition is None or transition[1] != EvalRun.Status.PASS:
            continue
        lines.append(f"- recovery: {_safe_text(event.subject, 128)}")
    return _cap_section(lines), len(events)


def _integrity() -> tuple[list[str], int]:
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
    return _cap_section(lines), len(lines)


def _truncate_digest(text: str) -> str:
    if len(text) <= MAX_DIGEST_CHARS:
        return text
    lines = text.splitlines()
    for keep in range(len(lines) - 1, -1, -1):
        omitted = len(lines) - keep
        candidate = "\n".join([*lines[:keep], f"… +{omitted} lines omitted"])
        if len(candidate) <= MAX_DIGEST_CHARS:
            return candidate
    return "… +1 lines omitted"


def render_steward_daily_digest(
    *,
    now: datetime | None = None,
) -> tuple[str, dict[str, int]]:
    now = now or timezone.now()
    last_digest = DigestRecord.objects.order_by("-sent_at", "-id").first()
    since = last_digest.sent_at if last_digest else now - timedelta(hours=24)

    sections = [
        ("NEEDS YOU", *_needs_you(now)),
        ("STALLED", *_stalled(now)),
        ("SLO / EVALS", *_slo_and_evals(now, since)),
        ("CHANGES (24h)", *_changes(since)),
        ("INTEGRITY", *_integrity()),
    ]
    stats = {
        "needs_you": sections[0][2],
        "stalled": sections[1][2],
        "slo_evals": sections[2][2],
        "changes": sections[3][2],
        "integrity": sections[4][2],
    }

    rendered = ["STEWARD DAILY FACTS", now.strftime("%Y-%m-%d UTC")]
    if any(lines for _, lines, _ in sections):
        for title, lines, _ in sections:
            if not lines:
                continue
            rendered.extend(["", title, *lines])
    else:
        armed = Expectation.objects.filter(state=Expectation.State.ARMED).count()
        sweep_state = AlertState.objects.filter(fingerprint=_SWEEP_LIVENESS_FINGERPRINT).first()
        sweep_age = _age_label(
            now,
            sweep_state.last_sent_at if sweep_state else None,
        )
        rendered.extend(
            [
                "",
                "ALL QUIET",
                f"All quiet — {armed} expectations armed, last sweep {sweep_age}.",
            ]
        )
    return _truncate_digest("\n".join(rendered)), stats


def run_steward_daily_digest() -> dict[str, object]:
    """Collect fresh eval facts, render, deliver, and record the daily digest."""
    collect_eval_evidence()
    rendered_at = timezone.now()
    text, stats = render_steward_daily_digest(now=rendered_at)
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
    record = DigestRecord(
        # This is the render cutoff as well as the send attempt time. Using the
        # post-network timestamp would create a gap for evidence arriving while
        # Telegram/Mailgun was in flight.
        sent_at=rendered_at,
        delivery=delivery,
        body=text,
        stats=stats,
    )
    record.full_clean()
    record.save()
    return {
        "delivery": delivery,
        "digest_id": record.id,
        "chars": len(text),
        "stats": stats,
    }
