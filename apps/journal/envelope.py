"""USER.md sections sourced from journal Documents.

Three sections live here because they all derive from ``Document``:

- **Active goals** — ``Document(kind=GOAL, slug="goals")`` body, char-capped.
- **Open tasks** — open ``- [ ]`` items from ``Document(kind=TASKS, slug="tasks")``.
- **Recent journal** — last few daily-note Documents (excluding today, which is
  volatile and loaded by the agent via ``nbhd_daily_note_get``).

Each registers as its own section so they appear under separate headings,
but all share the ``Document`` model for refresh triggers — a single Document
save triggers one debounced USER.md push that re-renders everything.
"""

from __future__ import annotations

from datetime import date as _date

from apps.journal.models import Document
from apps.orchestrator.envelope_registry import register_section
from apps.tenants.models import Tenant

# Cached starter content so we can detect un-curated seed documents and
# treat them as empty for envelope purposes. Imported lazily to avoid the
# circular ``apps.journal.envelope -> apps.journal.services`` problem at
# Django startup.
_STARTER_CACHE: dict[str, str] = {}


def _starter_markdown(slug: str) -> str:
    if not _STARTER_CACHE:
        from apps.journal.services import STARTER_DOCUMENT_TEMPLATES

        _STARTER_CACHE.update({t["slug"]: t["markdown"] for t in STARTER_DOCUMENT_TEMPLATES})
    return _STARTER_CACHE.get(slug, "")


def _starter_task_lines() -> frozenset[str]:
    seed = _starter_markdown("tasks")
    return frozenset(line.strip() for line in seed.splitlines() if line.lstrip().startswith("- [ ]"))


@register_section(
    key="goals",
    heading="## Active goals",
    enabled=lambda t: True,
    refresh_on=(Document,),
    order=20,
)
def render_goals(tenant: Tenant, *, max_chars: int = 1500) -> str:
    """Active goals doc body, char-capped, skipping unmodified starter seed."""
    doc = Document.objects.filter(tenant=tenant, kind=Document.Kind.GOAL, slug="goals").first()
    if not doc:
        return ""
    md = (doc.markdown or "").strip()
    if not md:
        return ""
    starter = _starter_markdown("goals").strip()
    if starter and md == starter:
        return ""
    if len(md) > max_chars:
        return md[:max_chars].rstrip() + "\n_(truncated — see goals doc for full text)_"
    return md


@register_section(
    key="tasks",
    heading="## Open tasks",
    enabled=lambda t: True,
    refresh_on=(),  # Document already triggers via the goals section
    order=30,
)
def render_open_tasks(tenant: Tenant, *, max_items: int = 25) -> str:
    """Open ``- [ ]`` items from the tasks doc, filtering starter placeholders."""
    doc = Document.objects.filter(tenant=tenant, kind=Document.Kind.TASKS, slug="tasks").first()
    if not doc:
        return ""
    starter_lines = _starter_task_lines()
    open_items = [
        line
        for line in (doc.markdown or "").splitlines()
        if line.lstrip().startswith("- [ ]") and line.strip() not in starter_lines
    ]
    if not open_items:
        return ""
    if len(open_items) > max_items:
        kept = open_items[:max_items]
        return "\n".join(kept) + f"\n_(+{len(open_items) - max_items} more open tasks in tasks doc)_"
    return "\n".join(open_items)


@register_section(
    key="recent_journal",
    heading="## Recent journal",
    enabled=lambda t: True,
    refresh_on=(),  # Document covered above
    order=70,
)
def render_recent_journal(tenant: Tenant, *, limit: int = 3, preview_chars: int = 250) -> str:
    """Last few daily-note Documents, excluding today (volatile within a day)."""
    today_iso = _date.today().isoformat()
    docs = list(
        Document.objects.filter(tenant=tenant, kind=Document.Kind.DAILY)
        .exclude(slug=today_iso)
        .order_by("-updated_at")[:limit]
    )
    if not docs:
        return ""

    lines: list[str] = []
    for doc in docs:
        title = (doc.title or doc.slug or "(untitled)").strip()
        body = (doc.markdown or "").strip()
        if not body:
            continue
        first = body.splitlines()[0].strip().lstrip("# ").strip()
        rest = " ".join(body.replace(first, "", 1).split())
        preview = (first + " — " + rest).strip(" —")
        if len(preview) > preview_chars:
            preview = preview[: preview_chars - 1].rstrip() + "…"
        lines.append(f"- **{title}**: {preview}")

    return "\n".join(lines)
