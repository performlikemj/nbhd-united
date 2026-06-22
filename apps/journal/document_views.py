"""User-facing Document API views (Journal v2)."""

from __future__ import annotations

import datetime
from collections import defaultdict

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.cache import tenant_cache
from apps.tenants.models import Tenant

from .document_serializers import (
    DocumentAppendSerializer,
    DocumentCreateSerializer,
    DocumentListSerializer,
    DocumentSerializer,
)
from .models import Document
from .services import (
    get_default_template as get_tenant_template,
)
from .services import (
    materialize_sections_markdown,
    seed_default_templates_for_tenant,
)
from .templates_md import (
    daily_note_context,
    render_template,
)
from .templates_md import (
    get_default_template as get_static_template,
)


def _get_tenant(user) -> Tenant:
    try:
        return user.tenant
    except Tenant.DoesNotExist as exc:
        raise Http404("Tenant not found.") from exc


import re

# Allow uppercase (ISO week format uses W, e.g. 2026-W09) and forward
# slashes (compound path slugs like week-ahead/2026-W09 via <path:slug>)
_VALID_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-/]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_slug(kind: str, slug: str) -> None:
    """Raise ValidationError if the slug is invalid for the given kind."""
    if not slug or not _VALID_SLUG_RE.match(slug):
        raise serializers.ValidationError(f"Invalid slug: {slug!r}")
    if kind == "daily" and not _DATE_RE.match(slug):
        raise serializers.ValidationError(f"Daily note slug must be a date (YYYY-MM-DD), got: {slug!r}")
    if kind == "daily":
        try:
            datetime.date.fromisoformat(slug)
        except ValueError:
            raise serializers.ValidationError(f"Invalid date: {slug!r}")


def _get_or_create_document(tenant: Tenant, kind: str, slug: str) -> Document:
    """Get or create a document, applying default template for new docs."""
    _validate_slug(kind, slug)
    doc, created = Document.objects.get_or_create(
        tenant=tenant,
        kind=kind,
        slug=slug,
        defaults={
            "title": _default_title(kind, slug),
            "markdown": _default_markdown(kind, slug, tenant=tenant),
        },
    )
    return doc


def _default_title(kind: str, slug: str) -> str:
    """Generate a human-readable title for a new document."""
    if kind == "daily":
        try:
            d = datetime.date.fromisoformat(slug)
            weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            return f"{d} ({weekday_names[d.weekday()]})"
        except ValueError:
            return slug
    if kind == "weekly":
        return f"Weekly Review — {slug}"
    if kind == "monthly":
        return f"Monthly Review — {slug}"
    if kind == "memory":
        return "Memory"
    if kind == "tasks":
        return "Tasks"
    if kind == "ideas":
        return "Ideas"
    if kind == "goal":
        return "Goals"
    if kind == "project":
        return slug.replace("-", " ").title()
    return slug


def _default_markdown(kind: str, slug: str, tenant=None) -> str:
    """Generate default markdown content for a new document.

    For daily notes, uses the tenant's NoteTemplate sections when available
    so that the document matches the user's customised template.
    """
    if kind == "daily" and tenant is not None:
        try:
            d = datetime.date.fromisoformat(slug)
        except ValueError:
            d = None

        if d is not None:
            note_template = get_tenant_template(tenant=tenant)
            if note_template is None:
                result = seed_default_templates_for_tenant(tenant=tenant)
                note_template = result["template"]
            if note_template is not None:
                return materialize_sections_markdown(
                    note_date=d,
                    sections=note_template.sections,
                    template_name=note_template.name,
                )

    # Fallback to static templates for non-daily kinds or when no tenant
    static = get_static_template(kind)
    if not static:
        return ""

    if kind == "daily":
        try:
            d = datetime.date.fromisoformat(slug)
            return render_template(static, daily_note_context(d))
        except ValueError:
            return static

    context = {"date": slug, "title": _default_title(kind, slug)}
    return render_template(static, context)


class DocumentListCreateView(APIView):
    """GET /api/v1/journal/documents/?kind=daily
    POST /api/v1/journal/documents/
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant(request.user)
        kind = request.query_params.get("kind")

        queryset = Document.objects.filter(tenant=tenant)
        if kind:
            queryset = queryset.filter(kind=kind)

        # For daily notes, support date range
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if kind == "daily":
            if date_from:
                queryset = queryset.filter(slug__gte=date_from)
            if date_to:
                queryset = queryset.filter(slug__lte=date_to)
            queryset = queryset.order_by("-slug")
        else:
            queryset = queryset.order_by("-updated_at")

        serializer = DocumentListSerializer(queryset, many=True)
        return Response(serializer.data)

    def post(self, request):
        tenant = _get_tenant(request.user)
        serializer = DocumentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        doc, created = Document.objects.get_or_create(
            tenant=tenant,
            kind=data["kind"],
            slug=data["slug"],
            defaults={
                "title": data["title"],
                "markdown": data.get("markdown") or _default_markdown(data["kind"], data["slug"], tenant=tenant),
            },
        )

        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(DocumentSerializer(doc).data, status=status_code)


def _synthesize_tasks_markdown(tenant: Tenant) -> str:
    """Render typed ``Task`` rows for a tenant as a tasks-document markdown blob.

    Used by ``DocumentDetailView.get`` to keep the existing /journal/tasks
    UI accurate when ``experimental_typed_journal_lifecycle`` is on (legacy
    ``Document(kind=tasks).markdown`` is preserved as archive but no longer
    the source of truth).
    """
    from .models import Task

    qs = list(
        Task.objects.filter(tenant=tenant).select_related("parent_task").order_by("status", "due_date", "-updated_at")
    )
    if not qs:
        return "# Tasks\n\n_No tasks yet._\n"

    by_status: dict[str, list[Task]] = defaultdict(list)
    for t in qs:
        by_status[t.status].append(t)

    def render_task(task: Task, indent: int = 0) -> str:
        prefix = "  " * indent
        if task.status == Task.Status.DONE:
            mark = "x"
        elif task.status == Task.Status.SKIPPED:
            mark = "~"
        elif task.status == Task.Status.DEFERRED:
            mark = "→"
        else:
            mark = " "
        due = f" _(due {task.due_date.isoformat()})_" if task.due_date else ""
        lines = [f"{prefix}- [{mark}] {task.title}{due}"]
        for child in task.subtasks.order_by("status", "-updated_at"):
            lines.append(render_task(child, indent + 1))
        return "\n".join(lines)

    sections = []
    section_order = [
        (Task.Status.OPEN, "Open"),
        (Task.Status.IN_PROGRESS, "In progress"),
        (Task.Status.DEFERRED, "Deferred"),
        (Task.Status.DONE, "Done"),
        (Task.Status.SKIPPED, "Skipped"),
    ]
    for status_key, label in section_order:
        items = [t for t in by_status.get(status_key, []) if t.parent_task_id is None]
        if not items:
            continue
        sections.append(f"## {label}\n\n" + "\n".join(render_task(t) for t in items))

    return "# Tasks\n\n" + "\n\n".join(sections) + "\n"


def _synthesize_goals_markdown(tenant: Tenant) -> str:
    """Render typed ``Goal`` rows for a tenant as a goals-document markdown blob."""
    from .models import Goal

    qs = list(Goal.objects.filter(tenant=tenant).order_by("status", "target_date", "-updated_at"))
    if not qs:
        return "# Goals\n\n_No goals yet._\n"

    by_status: dict[str, list[Goal]] = defaultdict(list)
    for g in qs:
        by_status[g.status].append(g)

    def render_goal(g: Goal) -> str:
        bullet = [f"### {g.title}"]
        if g.target_date:
            bullet.append(f"- Target: {g.target_date.isoformat()}")
        if g.status == Goal.Status.ACHIEVED and g.achieved_at:
            bullet.append(f"- Achieved: {g.achieved_at.date().isoformat()}")
        if g.description:
            bullet.append("")
            bullet.append(g.description)
        return "\n".join(bullet)

    sections = []
    section_order = [
        (Goal.Status.ACTIVE, "Active"),
        (Goal.Status.ACHIEVED, "Achieved"),
        (Goal.Status.ABANDONED, "Abandoned"),
    ]
    for status_key, label in section_order:
        items = by_status.get(status_key, [])
        if not items:
            continue
        sections.append(f"## {label}\n\n" + "\n\n".join(render_goal(g) for g in items))

    return "# Goals\n\n" + "\n\n".join(sections) + "\n"


class DocumentDetailView(APIView):
    """GET/PATCH/DELETE /api/v1/journal/documents/<kind>/<slug>/"""

    permission_classes = [IsAuthenticated]

    @tenant_cache(ttl=60, tag="journal")
    def get(self, request, kind: str, slug: str):
        tenant = _get_tenant(request.user)
        _validate_slug(kind, slug)
        # Singletons (tasks, ideas, memory) auto-create on GET for convenience
        singleton_kinds = {"tasks", "ideas", "memory"}
        if kind in singleton_kinds:
            doc = _get_or_create_document(tenant, kind, slug)
        else:
            try:
                doc = Document.objects.get(tenant=tenant, kind=kind, slug=slug)
            except Document.DoesNotExist:
                return Response(
                    {"error": "not_found", "detail": "Document not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

        # Typed-lifecycle synthesis: when the flag is on, the source of
        # truth for tasks + goals is the typed Task / Goal rows, not the
        # legacy Document(kind=tasks|goal).markdown archive. Replace the
        # markdown in the response so the existing journal UI shows
        # current state.
        if getattr(tenant, "experimental_typed_journal_lifecycle", False):
            if kind == "tasks":
                doc.markdown = _synthesize_tasks_markdown(tenant)
            elif kind == "goal":
                doc.markdown = _synthesize_goals_markdown(tenant)
        return Response(DocumentSerializer(doc).data)

    def patch(self, request, kind: str, slug: str):
        tenant = _get_tenant(request.user)

        markdown = request.data.get("markdown")
        title = request.data.get("title")

        # Typed-lifecycle guard: when the flag is on, tasks/goal docs are
        # rendered from typed rows on GET (DocumentDetailView.get), so a
        # markdown write here would be silently discarded on the next read.
        # Reject it instead of losing the user's edit — point them at the
        # typed write endpoints (apps/journal/lifecycle_views.py).
        if (
            markdown is not None
            and kind in {"tasks", "goal"}
            and getattr(tenant, "experimental_typed_journal_lifecycle", False)
        ):
            return Response(
                {
                    "error": "typed_lifecycle_readonly",
                    "detail": (
                        "Tasks and goals are managed as typed records. Update them via "
                        "/api/v1/journal/tasks/<id>/ or /api/v1/journal/goals/<id>/, "
                        "not by editing this document."
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )

        doc = _get_or_create_document(tenant, kind, slug)
        if markdown is not None:
            doc.markdown = markdown
        if title is not None:
            doc.title = title

        doc.save()
        return Response(DocumentSerializer(doc).data)

    def delete(self, request, kind: str, slug: str):
        if kind == "daily":
            return Response(
                {"error": "forbidden", "detail": "Daily notes cannot be deleted."},
                status=status.HTTP_403_FORBIDDEN,
            )
        tenant = _get_tenant(request.user)
        try:
            doc = Document.objects.get(tenant=tenant, kind=kind, slug=slug)
        except Document.DoesNotExist:
            raise Http404("Document not found.")
        doc.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DocumentClearView(APIView):
    """POST /api/v1/journal/documents/<kind>/<slug>/clear/

    Resets a document's markdown to empty. Used for daily notes and
    singletons (tasks, ideas, memory) where the record should persist
    but the content should be wiped.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, kind: str, slug: str):
        tenant = _get_tenant(request.user)
        _validate_slug(kind, slug)
        try:
            doc = Document.objects.get(tenant=tenant, kind=kind, slug=slug)
        except Document.DoesNotExist:
            raise Http404("Document not found.")
        doc.markdown = ""
        doc.save()
        return Response(DocumentSerializer(doc).data)


class DocumentAppendView(APIView):
    """POST /api/v1/journal/documents/<kind>/<slug>/append/

    Appends timestamped content to a document (used for quick log).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, kind: str, slug: str):
        tenant = _get_tenant(request.user)
        serializer = DocumentAppendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        doc = _get_or_create_document(tenant, kind, slug)

        with transaction.atomic():
            # Re-read under a row lock to serialise concurrent appends and
            # prevent a lost-update when two writers hit the same document.
            doc = Document.objects.select_for_update().get(pk=doc.pk)
            time_str = data.get("time") or timezone.now().strftime("%H:%M")
            entry_block = f"\n\n### {time_str} — MJ\n{data['content'].strip()}\n"
            doc.markdown = (doc.markdown or "").rstrip() + entry_block
            doc.save(update_fields=["markdown", "updated_at"])

        return Response(DocumentSerializer(doc).data, status=status.HTTP_201_CREATED)


class TodayView(APIView):
    """GET /api/v1/journal/today/ — convenience for today's daily note."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant(request.user)
        today = timezone.now().date()
        doc = _get_or_create_document(tenant, "daily", str(today))
        return Response(DocumentSerializer(doc).data)


class SidebarTreeView(APIView):
    """GET /api/v1/journal/tree/ — returns the sidebar tree structure."""

    permission_classes = [IsAuthenticated]

    @tenant_cache(ttl=120, tag="sidebar")
    def get(self, request):
        tenant = _get_tenant(request.user)
        today = str(timezone.now().date())
        documents = Document.objects.filter(tenant=tenant).values("kind", "slug", "title", "updated_at")

        # Group by kind
        tree: dict[str, list] = defaultdict(list)
        for doc in documents:
            # Hide future daily notes from sidebar
            if doc["kind"] == "daily" and doc["slug"] > today:
                continue
            tree[doc["kind"]].append(
                {
                    "slug": doc["slug"],
                    "title": doc["title"],
                    "updated_at": doc["updated_at"].isoformat() if doc["updated_at"] else None,
                }
            )

        # Sort daily notes by slug (date) descending
        if "daily" in tree:
            tree["daily"].sort(key=lambda x: x["slug"], reverse=True)

        # Sort weekly reviews by slug (YYYY-MM-DD week-start date) descending so
        # the [:12] cap keeps the 12 most-recent weeks, not the 12 most-recently-edited.
        if "weekly" in tree:
            tree["weekly"].sort(key=lambda x: x["slug"], reverse=True)

        # Define the sidebar structure
        sidebar = [
            {"kind": "daily", "label": "Daily Notes", "items": tree.get("daily", [])[:30]},
            {"kind": "weekly", "label": "Weekly Reviews", "items": tree.get("weekly", [])[:12]},
            {"kind": "tasks", "label": "Tasks", "items": tree.get("tasks", [])},
            {"kind": "goals", "label": "Goals", "items": tree.get("goal", [])},
            {"kind": "ideas", "label": "Ideas", "items": tree.get("ideas", [])},
            {"kind": "project", "label": "Projects", "items": tree.get("project", [])},
            {"kind": "memory", "label": "Memory", "items": tree.get("memory", [])},
        ]

        return Response(sidebar)
