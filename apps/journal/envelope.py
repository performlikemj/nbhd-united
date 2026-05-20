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
    """Active goals — typed Goal rows when present, else legacy Document fallback.

    Dual-read keeps the envelope correct during the journal-typed-lifecycle
    rollout: if the tenant has migrated Goal rows, render those; otherwise
    fall back to the legacy ``Document(kind=goal, slug=goals)`` markdown.
    Stale tenants are unaffected.
    """
    # Local import — Goal isn't used at module load time for the legacy path
    # and the lint-on-Edit hook strips unused module-level imports.
    from .models import Goal

    active_goals = list(Goal.objects.filter(tenant=tenant, status=Goal.Status.ACTIVE).order_by("-updated_at"))
    if active_goals:
        lines: list[str] = []
        running = 0
        for goal in active_goals:
            line = f"- **{goal.title.strip()}**"
            if goal.pillar:
                line += f" _(pillar: {goal.pillar})_"
            if goal.target_date:
                line += f" _(target: {goal.target_date.isoformat()})_"
            description = (goal.description or "").strip()
            if description:
                first = description.splitlines()[0].strip().lstrip("# ").strip()
                if first:
                    line += f" — {first}"
            if running + len(line) > max_chars:
                lines.append(f"_(+{len(active_goals) - len(lines)} more goals — see Horizons)_")
                break
            lines.append(line)
            running += len(line) + 1
        return "\n".join(lines)

    # Legacy fallback — Document-backed goals for stale tenants.
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
    """Open tasks — typed Task rows when present, else legacy Document markdown.

    Dual-read mirrors ``render_goals`` for the same rollout reason.
    """
    from .models import Task

    open_tasks = list(
        Task.objects.filter(tenant=tenant, status=Task.Status.OPEN).order_by("due_date", "-created_at")[:max_items]
    )
    total_open = Task.objects.filter(tenant=tenant, status=Task.Status.OPEN).count()
    if open_tasks:
        lines: list[str] = []
        for task in open_tasks:
            line = f"- [ ] {task.title.strip()}"
            if task.due_date:
                line += f" _(due {task.due_date.isoformat()})_"
            if task.pillar:
                line += f" _({task.pillar})_"
            lines.append(line)
        if total_open > len(open_tasks):
            lines.append(f"_(+{total_open - len(open_tasks)} more open tasks)_")
        return "\n".join(lines)

    # Legacy fallback — Document-backed tasks for stale tenants.
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
