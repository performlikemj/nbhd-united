"""USER.md ``Core — mindfulness state`` section.

Last completed meditation + this-week count. Gated on ``tenant.core_enabled``.
Registering MeditationSession as a ``refresh_on`` trigger auto-wires USER.md
refresh whenever a session is created, becomes ready, or is completed.
"""

from __future__ import annotations

from datetime import timedelta as _timedelta

from apps.core.models import MeditationSession, MeditationStatus
from apps.orchestrator.envelope_registry import register_section
from apps.tenants.models import Tenant


@register_section(
    key="core",
    heading="## Core — mindfulness state",
    enabled=lambda t: getattr(t, "core_enabled", False),
    refresh_on=(MeditationSession,),
    order=41,
)
def render_core(tenant: Tenant, *, max_chars: int = 600) -> str:
    from django.db.models.functions import TruncDate

    from apps.common.tenant_tz import tenant_today, tenant_tz

    today = tenant_today(tenant)
    completed = MeditationSession.objects.filter(
        tenant=tenant, status=MeditationStatus.DONE, completed_at__isnull=False
    ).annotate(completion_date=TruncDate("completed_at", tzinfo=tenant_tz(tenant)))
    ready = MeditationSession.objects.filter(tenant=tenant, status=MeditationStatus.READY).first()
    last = completed.order_by("-completed_at").first()
    sections: list[str] = []
    if last:
        line = f"- **Last completed sit**: {last.title or 'untitled'} ({last.completion_date.isoformat()})"
        if last.theme:
            line += f" — {last.theme[:120]}"
        sections.append(line)
    if last or ready:
        count = completed.filter(completion_date__gte=today - _timedelta(days=6), completion_date__lte=today).count()
        sections.append(f"- **This week**: {count} completed sit(s)")
    if ready:
        sections.append(f"- **Ready to play**: {ready.title or 'untitled'} (not yet listened)")
    return "\n".join(sections)[:max_chars].rstrip()
