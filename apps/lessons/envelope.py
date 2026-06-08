"""USER.md lesson/constellation sections.

* ``Recent lessons``           — top approved lessons by recency (one-liners).
* ``Constellation — active stars`` — the enriched context the user has been
  working through (pinned notes, reflections, tutoring signals). Assembled in
  :mod:`apps.lessons.agent_context` so it stays in lock-step with the
  ``nbhd_journal_context`` and ``nbhd_constellation_notes`` surfaces.
"""

from __future__ import annotations

from apps.lessons.agent_context import render_constellation_envelope
from apps.lessons.models import Lesson, StarJournalEntry
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


@register_section(
    key="constellation_activity",
    heading="## Constellation — active stars",
    enabled=lambda t: True,
    # TutoringSession has no tenant FK (the registry resolves tenant via
    # tenant_id/tenant), so it can't drive a refresh directly — but
    # ``end_tutoring`` saves the star (a Lesson write) and reflections are
    # StarJournalEntry writes, so those two cover every activity that changes
    # this section.
    refresh_on=(Lesson, StarJournalEntry),
    order=62,
)
def render_constellation_activity(tenant: Tenant) -> str:
    """Stars the user has recently worked through, with notes/reflections/signals."""
    return render_constellation_envelope(tenant)
