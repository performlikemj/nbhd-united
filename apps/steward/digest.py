from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.steward.collectors.evals import collect_eval_evidence
from apps.steward.facts import compose_steward_facts
from apps.steward.models import DigestRecord
from apps.steward.sanitize import safe_text as _safe_text

MAX_DIGEST_CHARS = 3500
MAX_SECTION_LINES = 10
MAX_RENDERED_SUBJECT_CHARS = 80
CLOSING_HINT = "Reply on Telegram or run: python manage.py steward_ack <expectation_id> / steward_decide"


def _age_label(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "unknown"
    age_seconds = max(0, age_seconds)
    if age_seconds < 3600:
        return f"{age_seconds // 60}m ago"
    if age_seconds < 86400:
        return f"{age_seconds // 3600}h ago"
    return f"{age_seconds // 86400}d ago"


def _needs_you_lines(facts: dict[str, Any]) -> list[str]:
    lines = []
    waiting = []
    for item in facts["needs_you"]:
        if item["remind_today"]:
            detail = f" — {item['context']}" if item["context"] else ""
            lines.append(f"- {item['title']} — {item['waiting_days']}d waiting{detail}")
        else:
            waiting.append(item)
    if waiting:
        oldest = max(waiting, key=lambda item: item["waiting_days"])
        lines.append(
            f"- {len(waiting)} items waiting (next reminder for oldest in {oldest['next_reminder_days']} days)"
        )
    return lines


def _stalled_lines(facts: dict[str, Any]) -> list[str]:
    lines = []
    for item in facts["stalled"]:
        overdue = _age_label(item["overdue_seconds"]).removesuffix(" ago")
        alerted = ""
        if item["already_alerted"]:
            alerted = f"; alerted {_age_label(item['alert_age_seconds'])}"
        lines.append(
            f"- {_safe_text(item['subject'], MAX_RENDERED_SUBJECT_CHARS)} — {overdue} overdue{alerted} — {item['hint']}"
        )
    return lines


def _train_lines(facts: dict[str, Any]) -> list[str]:
    lines = []
    for item in facts["trains"]:
        age_days = item["phase_age_seconds"] // 86400
        base = f"- {item['product']} {item['version_string']}: {item['phase']} ({age_days}d)"
        lines.append(f"{base} — {item['hint']}" if item["hint"] else base)
    return lines


def _slo_eval_lines(facts: dict[str, Any]) -> list[str]:
    lines = []
    for item in facts["failing_evals"]:
        state = "failing" if item["status"] == "fail" else "errored"
        lines.append(
            f"- EVAL {_safe_text(item['suite'], MAX_RENDERED_SUBJECT_CHARS)}: "
            f"{state} since {_age_label(item['age_seconds'])} (run {item['run_id']}) "
            f"— {item['hint']}"
        )
    for item in facts["slo_breaches"]:
        lines.append(
            f"- SLO {_safe_text(item['case_id'], MAX_RENDERED_SUBJECT_CHARS)}: "
            f"{item['score']} vs {item['threshold']} ({item['breach_days']} breach days) "
            f"— {item['hint']}"
        )
    return lines


def _openrouter_lines(facts: dict[str, Any]) -> list[str]:
    lines = []
    for item in facts["openrouter_severe"]:
        prefix = f"- {item['scope']} {_safe_text(item['model'], MAX_RENDERED_SUBJECT_CHARS)}: "
        if item["kind"] == "null_rate":
            detail = f"null finish_reason {item['current_pct']:.2f}% (> {item['threshold_pct']:.2f}%)"
        else:
            detail = (
                f"tool_calls {item['current_pct']:.2f}% vs {item['baseline_pct']:.2f}% "
                f"({item['drop_pts']:.2f} pts drop)"
            )
        lines.append(f"{prefix}{detail} — {item['hint']}")
    return lines


def _repo_lines(facts: dict[str, Any]) -> list[str]:
    return [
        f"- {item['repo']} #{item['number']} — {_safe_text(item['title'], 60)} — "
        f"{item['quiet_seconds'] // 86400}d quiet — {item['hint']}"
        for item in facts["stale_prs"]
    ]


def _integrity_lines(facts: dict[str, Any]) -> list[str]:
    lines = []
    for item in facts["integrity"]:
        if item["id"].startswith("collector:"):
            lines.append(f"- collector {item['collector']}: {item['issue']}")
        else:
            lines.append(f"- {item['title']} — {item['issue']}")
    return lines


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
        rendered += _render_section_block(title, lines, count, budget=section_budget)
    return rendered + footer_block


def _latest_watermark(now: datetime) -> datetime:
    record = (
        DigestRecord.objects.filter(
            delivery__in=[
                DigestRecord.Delivery.DELIVERED,
                DigestRecord.Delivery.RECORDED,
            ]
        )
        .order_by("-sent_at", "-id")
        .first()
    )
    return record.sent_at if record else now - timedelta(hours=24)


def render_steward_daily_digest(
    *,
    now: datetime | None = None,
    facts: dict[str, Any] | None = None,
) -> tuple[str, dict[str, int]]:
    if facts is None:
        now = (now or timezone.now()).astimezone(UTC)
        facts = compose_steward_facts(now, _latest_watermark(now))

    generated_date = facts["generated_at"][:10]
    stats = dict(facts["stats"])
    sections = [
        ("NEEDS YOU", _needs_you_lines(facts), stats["needs_you"]),
        ("STALLED", _stalled_lines(facts), stats["stalled"]),
        ("TRAINS", _train_lines(facts), stats["trains"]),
        ("SLO / EVALS", _slo_eval_lines(facts), stats["slo_evals"]),
        ("OPENROUTER", _openrouter_lines(facts), stats["openrouter"]),
        ("REPOS", _repo_lines(facts), stats["repos"]),
        ("INTEGRITY", _integrity_lines(facts), stats["integrity"]),
    ]
    header = "\n".join(["STEWARD DAILY FACTS", f"{generated_date} UTC"])
    nonempty_sections = [section for section in sections if section[1]]
    if nonempty_sections:
        rendered = _render_budgeted_sections(header, nonempty_sections, footer=CLOSING_HINT)
    else:
        liveness = facts["liveness"]
        sweep_age = _age_label(liveness["last_sweep_age_seconds"])
        rendered = "\n".join(
            [
                header,
                "",
                "ALL QUIET",
                f"All quiet — {liveness['armed_expectations']} expectations armed, last sweep {sweep_age}.",
            ]
        )
    return rendered, stats


def run_steward_daily_digest() -> dict[str, object]:
    """Collect fresh facts, render them, and record the snapshot without delivery."""
    rendered_at = timezone.now().astimezone(UTC)
    period_date = rendered_at.date()
    collect_eval_evidence()
    facts = compose_steward_facts(rendered_at, _latest_watermark(rendered_at))
    text, stats = render_steward_daily_digest(facts=facts)

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
        if (
            record.delivery
            in {
                DigestRecord.Delivery.DELIVERED,
                DigestRecord.Delivery.RECORDED,
            }
            or record.body
        ):
            return {
                "delivery": record.delivery,
                "digest_id": record.id,
                "chars": len(record.body),
                "stats": record.stats,
                "skipped": True,
            }

        record.sent_at = timezone.now()
        record.delivery = DigestRecord.Delivery.RECORDED
        record.body = text
        record.stats = {**stats, "facts": facts}
        record.full_clean()
        record.save(update_fields=["sent_at", "delivery", "body", "stats"])
    return {
        "delivery": DigestRecord.Delivery.RECORDED,
        "digest_id": record.id,
        "chars": len(text),
        "stats": stats,
        "skipped": False,
    }
