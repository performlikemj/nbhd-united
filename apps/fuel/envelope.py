"""USER.md ``Fuel — fitness state`` section.

Today's planned workout, a computed 4-week trends digest (volume,
frequency-by-activity, recency, recent PRs — what a coach reasons from,
not a raw row dump), the single most-recent done session with measured
metrics + provenance, last body weight (with 7d delta), last sleep entry,
recent resting HR. Gated on ``tenant.fuel_enabled``.
"""

from __future__ import annotations

from datetime import timedelta as _timedelta

from apps.common.tenant_tz import tenant_today, tenant_tz
from apps.fuel.models import BodyWeightLog, RestingHeartRateLog, SleepLog, Workout, WorkoutStatus
from apps.orchestrator.envelope_registry import register_section
from apps.tenants.models import Tenant


@register_section(
    key="fuel",
    heading="## Fuel — fitness state",
    enabled=lambda t: getattr(t, "fuel_enabled", False),
    refresh_on=(Workout, BodyWeightLog, SleepLog, RestingHeartRateLog),
    order=40,
)
def render_fuel(tenant: Tenant, *, max_chars: int = 1200) -> str:
    # Local import: services imports back into fuel models, and pulling it
    # at module load would couple envelope registration to that chain.
    from apps.fuel.services import weekly_trends_digest

    # Tenant-local day, not server UTC — a JST tenant's "today" section
    # would otherwise flip at 09:00 local.
    today = tenant_today(tenant)
    sections: list[str] = []

    planned_today = Workout.objects.filter(
        tenant=tenant,
        status=WorkoutStatus.PLANNED,
        date=today,
    ).first()
    if planned_today:
        bits = [f"- **Today**: {planned_today.activity} ({planned_today.category})"]
        if planned_today.scheduled_at:
            local_at = planned_today.scheduled_at.astimezone(tenant_tz(tenant))
            bits[0] += f" at {local_at.strftime('%H:%M')}"
        if planned_today.duration_minutes:
            bits[0] += f" — {planned_today.duration_minutes} min"
        sections.append("\n".join(bits))

    # Computed 4-week trends — what a coach reasons from. Replaces the old
    # raw "last 3 sessions" dump (the least information-dense rows in the
    # section); the single most-recent session below keeps concrete colour.
    trends = weekly_trends_digest(tenant)
    if trends:
        sections.append(trends)

    last_done = (
        Workout.objects.filter(
            tenant=tenant,
            status=WorkoutStatus.DONE,
            date__gte=today - _timedelta(days=14),
        )
        .order_by("-date", "-created_at")
        .first()
    )
    if last_done:
        line = f"- **Last session**: {last_done.date.isoformat()} — {last_done.activity} ({last_done.category}"
        if last_done.rpe:
            line += f", RPE {last_done.rpe}"
        if last_done.duration_minutes:
            line += f", {last_done.duration_minutes}m"
        detail = last_done.detail_json if isinstance(last_done.detail_json, dict) else {}
        if isinstance(detail.get("distance_km"), int | float):
            line += f", {detail['distance_km']} km"
        if isinstance(detail.get("avg_hr"), int):
            line += f", {detail['avg_hr']} bpm avg"
        line += ")"
        # Provenance: HealthKit imports are measured watch data the model can
        # coach off; manual/chat logs are user-asserted. Only flag HK (the
        # signal the assistant most needs to disambiguate).
        if last_done.source == "healthkit":
            line += " · via Apple Health"
        sections.append(line)

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

    last_rhr = (
        RestingHeartRateLog.objects.filter(tenant=tenant, date__gte=today - _timedelta(days=3))
        .order_by("-date")
        .first()
    )
    if last_rhr:
        sections.append(f"- **Resting HR**: {last_rhr.bpm} bpm ({last_rhr.date.isoformat()})")

    if not sections:
        return ""

    body = "\n\n".join(sections)
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n_(truncated — call nbhd_fuel_summary for full state)_"
    return body
