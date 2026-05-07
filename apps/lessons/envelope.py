"""USER.md ``Recent lessons`` section — top approved lessons by recency."""

from __future__ import annotations

from apps.lessons.models import Lesson
from apps.orchestrator.envelope_registry import register_section
from apps.tenants.models import Tenant


@register_section(
    key="recent_lessons",
    heading="## Recent lessons",
    enabled=lambda t: True,
    refresh_on=(Lesson,),
    order=60,
)
def render_recent_lessons(tenant: Tenant, *, limit: int = 3) -> str:
    """Most recent approved lessons as one-line summaries."""
    lessons = list(Lesson.objects.filter(tenant=tenant, status="approved").order_by("-created_at")[:limit])
    if not lessons:
        return ""
    out: list[str] = []
    for lesson in lessons:
        text = (lesson.text or "").strip()
        if not text:
            continue
        first_line = text.splitlines()[0]
        if len(first_line) > 140:
            first_line = first_line[:137].rstrip() + "..."
        out.append(f"- {first_line}")
    return "\n".join(out)
