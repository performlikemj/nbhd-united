"""USER.md ``Core — mindfulness state`` section.

Last completed meditation + this-week count. Gated on ``tenant.core_enabled``.
Registering MeditationSession as a ``refresh_on`` trigger auto-wires USER.md
refresh whenever a session is created or flips to ready.
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
    from apps.common.tenant_tz import tenant_today

    today = tenant_today(tenant)  # tenant-local, to match the locally-stamped session dates
    ready = MeditationSession.objects.filter(tenant=tenant, status=MeditationStatus.READY)

    sections: list[str] = []
    last = ready.order_by("-date", "-created_at").first()
    if last:
        line = f"- **Last meditation**: {last.title or 'untitled'} ({last.date.isoformat()})"
        if last.theme:
            line += f" — {last.theme[:120]}"
        sections.append(line)

    count_7d = ready.filter(date__gte=today - _timedelta(days=7)).count()
    if count_7d:
        sections.append(f"- **This week**: {count_7d} session(s)")

    if not sections:
        return ""

    body = "\n".join(sections)
    if len(body) > max_chars:
        body = body[:max_chars].rstrip()
    return body
