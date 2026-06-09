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


def render_goals(tenant: Tenant, *, max_chars: int = 1500) -> str:
    """Active goals — full inline markdown.

    Used by callers that want the full list once, not the always-loaded
    USER.md path:

    - ``apps/router/poller.py._build_session_context_inner`` — session-start
      injection (fires on the first message after a 30-min gap), full list
      is helpful, paid once per session.
    - ``apps/integrations/runtime_views.py`` ``nbhd_journal_context`` — the
      agent's on-demand backbone-fetch tool, MUST return full content (a
      pointer here would create a circular tool-call loop).

    USER.md uses :func:`render_goals_summary` (registered via
    ``@register_section``) instead — a one-line summary + retrieval pointer
    so the always-loaded bootstrap stays small.
    """
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


def render_open_tasks(tenant: Tenant, *, max_items: int = 25) -> str:
    """Open tasks — full inline markdown.

    Same rationale as :func:`render_goals` — full content for session-
    start + backbone-fetch callers. USER.md uses
    :func:`render_open_tasks_summary`.
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
    key="goals",
    heading="## Active goals",
    enabled=lambda t: True,
    refresh_on=(Document,),
    order=20,
)
def render_goals_summary(tenant: Tenant) -> str:
    """One-line summary + retrieval pointer — for USER.md only.

    Previously USER.md rendered the full active-goal list inline (1–4 KB
    depending on tenant). Combined with similar sections this pushed
    USER.md past OpenClaw's 12 KB per-file bootstrap budget and silently
    truncated the tail. Per the OpenClaw workspace docs (docs.openclaw.ai
    → Agent Workspace), per-tenant dynamic state belongs in on-demand
    retrieval, not always-loaded bootstrap.

    The reconcile gate in AGENTS.md (PR #666) already requires the agent
    to call ``nbhd_reconcile_scan`` before replying to a goal-related
    claim, so the tools-first pattern is established; the always-loaded
    list was bonus context the agent could (and should) re-derive on
    demand via ``nbhd_goal_list``.

    Session-start context (apps/router/poller.py) and the
    ``nbhd_journal_context`` backbone-fetch tool still use the full
    :func:`render_goals` — those are paid-once paths where the data IS
    valuable inline.
    """
    from .models import Goal

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

    # Legacy Document fallback — surface a pointer, not the doc body.
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
def render_open_tasks_summary(tenant: Tenant) -> str:
    """One-line summary + retrieval pointer — for USER.md only.

    Same rationale as :func:`render_goals_summary`. Full inline content
    for session-start + backbone-fetch lives in :func:`render_open_tasks`.
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

    # Legacy Document fallback — surface a pointer, not the doc body.
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
    key="conversation_digest",
    heading="## Conversation so far",
    enabled=lambda t: True,
    # NOT refresh_on=(ConversationTurn,): the registry's universal receiver
    # pushes with debounce 0, which would storm the file share on the
    # highest-frequency event in the system. ConversationTurn capture triggers
    # its own DEBOUNCED push instead (apps.router.conversation_capture).
    refresh_on=(),
    order=65,  # just above Recent journal (70)
)
def render_conversation_digest(tenant: Tenant) -> str:
    """Deterministic 'what the user actually discussed today + recent days'.

    Sourced from captured chat turns so ISOLATED cron sessions (Evening
    Check-in, Heartbeat, …) and any proactive turn see the conversation even
    when the agent never journaled it — the blind spot that produced
    "quiet day on the chat front" on days with substantive chats. Local import
    avoids an apps.journal → apps.router cycle at app boot.
    """
    from apps.router.conversation_capture import build_conversation_digest

    return build_conversation_digest(tenant)


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
