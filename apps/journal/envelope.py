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
def render_goals(tenant: Tenant) -> str:
    """One-line summary + retrieval pointer.

    Previously rendered the full active-goal list inline (1–4 KB depending
    on tenant), which combined with similar sections pushed USER.md past
    OpenClaw's 12 KB bootstrap budget and silently truncated the tail.
    Per the OpenClaw workspace docs, per-tenant dynamic state should be
    retrieved on demand — ``nbhd_goal_list`` exists for exactly this.

    The reconcile-gate in AGENTS.md (#666) already requires the agent to
    call ``nbhd_reconcile_scan`` before replying to a goal-related claim,
    so the tools-first pattern is established; the always-loaded list was
    bonus context the agent could (and should) re-derive on demand.
    """
    from .models import Goal

    # Typed Goal rows — preferred path.
    active_count = Goal.objects.filter(tenant=tenant, status=Goal.Status.ACTIVE).count()
    if active_count:
        last = (
            Goal.objects.filter(tenant=tenant, status=Goal.Status.ACTIVE)
            .order_by("-updated_at")
            .values_list("updated_at", flat=True)
            .first()
        )
        when = last.date().isoformat() if last else "unknown"
        return (
            f"_{active_count} active goal(s). Last edit: {when}. "
            f"Call `nbhd_goal_list({{status: 'active'}})` for current titles, pillars, target dates, "
            f"and descriptions; use `nbhd_goal_get({{goal_id}})` to drill into one._"
        )

    # Legacy Document fallback — only surfaces if the tenant hasn't migrated
    # to typed Goals yet. Render a pointer rather than dumping the full doc.
    doc = Document.objects.filter(tenant=tenant, kind=Document.Kind.GOAL, slug="goals").first()
    if doc and (doc.markdown or "").strip() and (doc.markdown or "").strip() != _starter_markdown("goals").strip():
        return (
            "_Legacy goals document present. "
            "Call `nbhd_document_get({kind: 'goal', slug: 'goals'})` to read; "
            "consider migrating to typed Goal rows via `nbhd_goal_create`._"
        )
    return ""


@register_section(
    key="tasks",
    heading="## Open tasks",
    enabled=lambda t: True,
    refresh_on=(),  # Document already triggers via the goals section
    order=30,
)
def render_open_tasks(tenant: Tenant) -> str:
    """One-line summary + retrieval pointer.

    Same rationale as :func:`render_goals` — the full task list was 3–4 KB
    in USER.md on every turn and silently truncated. ``nbhd_task_list``
    serves the agent on demand, the reconcile gate already requires the
    agent to query before replying to a task claim.
    """
    from .models import Task

    open_count = Task.objects.filter(tenant=tenant, status=Task.Status.OPEN).count()
    in_progress_count = Task.objects.filter(tenant=tenant, status=Task.Status.IN_PROGRESS).count()

    if open_count or in_progress_count:
        from datetime import timedelta

        from django.utils import timezone as tz

        due_soon = Task.objects.filter(
            tenant=tenant,
            status=Task.Status.OPEN,
            due_date__lte=(tz.now().date() + timedelta(days=7)),
            due_date__isnull=False,
        ).count()
        due_phrase = f", {due_soon} due this week" if due_soon else ""
        return (
            f"_{open_count} open, {in_progress_count} in-progress{due_phrase}. "
            f"Call `nbhd_task_list({{status: 'open'}})` for titles, due dates, and pillars; "
            f"`nbhd_task_list({{status: 'in_progress'}})` for what's underway._"
        )

    # Legacy Document fallback — only if tenant hasn't migrated.
    doc = Document.objects.filter(tenant=tenant, kind=Document.Kind.TASKS, slug="tasks").first()
    if not doc:
        return ""
    starter_lines = _starter_task_lines()
    open_items = [
        line
        for line in (doc.markdown or "").splitlines()
        if line.lstrip().startswith("- [ ]") and line.strip() not in starter_lines
    ]
    if open_items:
        return (
            f"_Legacy tasks document with {len(open_items)} open item(s). "
            f"Call `nbhd_document_get({{kind: 'tasks', slug: 'tasks'}})` to read; "
            f"migrate to typed Task rows via `nbhd_task_create`._"
        )
    return ""


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
