"""USER.md ``Fuel — fitness state`` section.

Today's planned workout, last 3 done sessions, last body weight (with 7d
delta), last sleep entry. Gated on ``tenant.fuel_enabled``.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import timedelta as _timedelta

from apps.fuel.models import BodyWeightLog, SleepLog, Workout, WorkoutStatus
from apps.orchestrator.envelope_registry import register_section
from apps.tenants.models import Tenant


@register_section(
    key="fuel",
    heading="## Fuel — fitness state",
    enabled=lambda t: getattr(t, "fuel_enabled", False),
    refresh_on=(Workout, BodyWeightLog, SleepLog),
    order=40,
)
def render_fuel(tenant: Tenant, *, max_chars: int = 1000) -> str:
    today = _date.today()
    sections: list[str] = []

    planned_today = Workout.objects.filter(
        tenant=tenant,
        status=WorkoutStatus.PLANNED,
        date=today,
    ).first()
    if planned_today:
        bits = [f"- **Today**: {planned_today.activity} ({planned_today.category})"]
        if planned_today.scheduled_at:
            bits[0] += f" at {planned_today.scheduled_at.astimezone().strftime('%H:%M %Z')}"
        if planned_today.duration_minutes:
            bits[0] += f" — {planned_today.duration_minutes} min"
        sections.append("\n".join(bits))

    recent_done = list(
        Workout.objects.filter(
            tenant=tenant,
            status=WorkoutStatus.DONE,
            date__gte=today - _timedelta(days=14),
        ).order_by("-date")[:3]
    )
    if recent_done:
        lines = ["**Recent sessions** (last 14d):"]
        for w in recent_done:
            line = f"- {w.date.isoformat()} — {w.activity} ({w.category}"
            if w.rpe:
                line += f", RPE {w.rpe}"
            if w.duration_minutes:
                line += f", {w.duration_minutes}m"
            line += ")"
            lines.append(line)
        sections.append("\n".join(lines))

    last_weight = BodyWeightLog.objects.filter(tenant=tenant).order_by("-date").first()
    if last_weight:
        weight_line = f"- **Body weight**: {last_weight.weight_kg} kg ({last_weight.date.isoformat()})"
        prior = (
            BodyWeightLog.objects.filter(tenant=tenant, date__lte=last_weight.date - _timedelta(days=6))
            .order_by("-date")
            .first()
        )
        if prior and prior.weight_kg != last_weight.weight_kg:
            delta = float(last_weight.weight_kg) - float(prior.weight_kg)
            sign = "+" if delta > 0 else ""
            weight_line += f" — {sign}{delta:.1f} kg vs {prior.date.isoformat()}"
        sections.append(weight_line)

    last_sleep = SleepLog.objects.filter(tenant=tenant).order_by("-date").first()
    if last_sleep:
        sleep_line = f"- **Last sleep**: {last_sleep.duration_hours}h ({last_sleep.date.isoformat()})"
        if last_sleep.quality is not None:
            sleep_line += f", quality {last_sleep.quality}/5"
        sections.append(sleep_line)

    if not sections:
        return ""

    body = "\n\n".join(sections)
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n_(truncated — call nbhd_fuel_summary for full state)_"
    return body
