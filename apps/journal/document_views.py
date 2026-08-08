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
from apps.common.llm_contracts import today_in_tenant_tz
from apps.tenants.models import Tenant

from .document_authoring import get_or_create_authored_document, merge_field_receipt, set_field_receipt
from .document_serializers import (
    DocumentAppendSerializer,
    DocumentCreateSerializer,
    DocumentListSerializer,
    DocumentSerializer,
)
from .md_utils import format_author_suffix
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


_DOCUMENT_STORE = "journal.Document"


def _author_owner_document(tenant, text: str, *, seam: str, field: str):
    """Route one owner-submitted Document field through the Layer-1 chokepoint.

    Owner writer class: full NER + ``MINT_ALL``, chat-ingress parity — the human
    is introducing the names. With the tenant flag OFF this is byte-identical to
    the legacy ``redact_user_message()`` these endpoints called before P3, so a
    flag-off tenant sees no behavior change (directive §A4).
    """
    from apps.pii.authoring import author_text

    return author_text(
        tenant,
        text,
        seam=seam,
        writer="owner",
        field=field,
        model_label=_DOCUMENT_STORE,
    )


import re

# Allow uppercase (ISO week format uses W, e.g. 2026-W09) and forward
# slashes (compound path slugs like week-ahead/2026-W09 via <path:slug>)
_VALID_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-/]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_slug(kind: str, slug: str) -> None:
    """Raise ValidationError if the slug is invalid for the given kind."""
    if not slug or not _VALID_SLUG_RE.match(slug):
        raise serializers.ValidationError(f"Invalid slug: {slug!r}")
    # URL-path endpoints (PATCH/append/clear/GET) pass the slug straight here, so
    # enforce the column limit too — otherwise an over-long slug 500s on the DB
    # insert instead of returning a clean 400 (matches validate_kind_slug).
    if len(slug) > 128:  # Document.slug max_length
        raise serializers.ValidationError(f"slug must be at most 128 characters (got {len(slug)})")
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
    doc, _created = get_or_create_authored_document(
        tenant,
        kind=kind,
        slug=slug,
        title=_default_title(kind, slug),
        markdown_factory=lambda: _default_markdown(kind, slug, tenant=tenant),
        seam="journal.document.default_body",
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

        serializer = DocumentListSerializer(queryset, many=True, context={"tenant": tenant})
        return Response(serializer.data)

    def post(self, request):
        tenant = _get_tenant(request.user)
        serializer = DocumentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Same slug guard as every other console write path (PATCH/append/clear,
        # via _get_or_create_document). POST was the one create path that skipped
        # it — letting a daily slug that isn't a real ISO date (e.g. the web UI's
        # "NaN-NaN-NaN" Invalid-Date artifact) or an NTFS-hostile slug persist a
        # garbage row that memory_sync then has to skip forever.
        _validate_slug(data["kind"], data["slug"])

        # POST is get-or-create, and a POST onto an existing slug returns that
        # row untouched. Author AFTER establishing it is a real create: authoring
        # is a detector pass that can MINT, so doing it first would grow the
        # entity map from text this request is about to throw away.
        existing = Document.objects.filter(tenant=tenant, kind=data["kind"], slug=data["slug"]).first()
        if existing is not None:
            return Response(DocumentSerializer(existing, context={"tenant": tenant}).data, status=status.HTTP_200_OK)

        # POST was the one owner Document write that stored its body RAW while
        # PATCH and append both re-redacted — so a document CREATED with a real
        # name in it handed that name straight to the agent (Document.markdown is
        # agent-readable via the runtime endpoints). Routing create through the
        # same chokepoint closes it; the named "owner POST raw fix".
        submitted_markdown = data.get("markdown")
        authored_title = _author_owner_document(
            tenant,
            data["title"],
            seam="journal.document.create",
            field="title",
        )
        receipts = {"title": authored_title.receipt}
        if submitted_markdown:
            authored_markdown = _author_owner_document(
                tenant,
                submitted_markdown,
                seam="journal.document.create",
                field="markdown",
            )
            markdown = authored_markdown.text
            receipts["markdown"] = authored_markdown.receipt
        else:
            # Server-rendered template body — no user text, so no receipt and no
            # NER pass. An absent key reads as "legacy/unknown", which is the
            # honest answer and the safe side of the A7 migration fence.
            markdown = _default_markdown(data["kind"], data["slug"], tenant=tenant)

        doc, created = Document.objects.get_or_create(
            tenant=tenant,
            kind=data["kind"],
            slug=data["slug"],
            defaults={
                "title": authored_title.text,
                "markdown": markdown,
                "pii_receipts": receipts,
            },
        )

        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(DocumentSerializer(doc, context={"tenant": tenant}).data, status=status_code)


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
        try:
            _validate_slug(kind, slug)
        except serializers.ValidationError:
            # A GET with a guessed identifier is a missing resource, even when
            # the guess is not a valid persisted slug. Writes still surface
            # malformed slugs as 400 validation errors.
            return Response(
                {"error": "not_found", "detail": "Document not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
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
        if getattr(tenant, "experimental_typed_journal_lifecycle", False) and kind in {"tasks", "goal"}:
            doc.markdown = _synthesize_tasks_markdown(tenant) if kind == "tasks" else _synthesize_goals_markdown(tenant)
            # The body served here is rendered from typed rows, not from the
            # archived column, so the column's markdown receipt does not
            # describe it. Drop it rather than ship a receipt for text the
            # client is not looking at. (In-memory only — never saved.)
            doc.pii_receipts = {
                field: receipt for field, receipt in (doc.pii_receipts or {}).items() if field != "markdown"
            }
        return Response(DocumentSerializer(doc, context={"tenant": tenant}).data)

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
        # Re-author owner input before persisting. The client round-trips
        # rehydrated (real-value) fields back here on save; without this pass
        # real PII would land in the agent-visible Document row. PATCH replaces
        # the whole column, so the submitted text's receipt IS the field's.
        #
        # ``authored_receipts`` deliberately starts EMPTY rather than from the
        # pre-lock row: it must hold only the fields this request authored. A
        # pre-lock snapshot written back under the lock would overwrite a
        # concurrent writer's receipt for a field this request never touched —
        # that writer's text would survive next to our receipt, and a receipt
        # that disagrees with its text rehydrates the wrong binding.
        #
        # Authoring stays OUTSIDE the transaction (invariants §8: no
        # out-of-process calls inside ``atomic()``).
        authored_text: dict[str, str] = {}
        authored_receipts: dict[str, dict] = {}
        for field, submitted in (("markdown", markdown), ("title", title)):
            if submitted is None:
                continue
            authored = _author_owner_document(
                tenant,
                submitted,
                seam="journal.document.patch",
                field=field,
            )
            authored_text[field] = authored.text
            authored_receipts[field] = authored.receipt

        with transaction.atomic():
            doc = Document.objects.select_for_update().get(pk=doc.pk)
            for field, value in authored_text.items():
                setattr(doc, field, value)
            doc.pii_receipts = {**(doc.pii_receipts or {}), **authored_receipts}
            doc.save(update_fields=[*authored_text, "pii_receipts", "updated_at"])
        return Response(DocumentSerializer(doc, context={"tenant": tenant}).data)

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
        # An emptied column has no placeholders left to describe. Leaving the
        # old receipt behind would advertise redactions that are no longer in
        # the text, and would let the A7 fence read a wiped field as verified.
        doc.pii_receipts = set_field_receipt(
            doc.pii_receipts,
            "markdown",
            _author_owner_document(tenant, "", seam="journal.document.clear", field="markdown").receipt,
        )
        doc.save()
        return Response(DocumentSerializer(doc, context={"tenant": tenant}).data)


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

        # Re-author owner input before persisting so the appended real PII does
        # not land in Document.markdown (agent-visible via RuntimeDailyNotesView).
        # Fail-open with a receipt: on a detector failure the chokepoint still
        # applies the deterministic known-value scrub and records `unconfirmed`.
        # Only the FRAGMENT is authored — the rest of the note was authored when
        # it was written, and re-running NER over a whole day's note per quick-log
        # would buy nothing.
        authored = _author_owner_document(
            tenant,
            data["content"].strip(),
            seam="journal.document.append",
            field="markdown",
        )
        content = authored.text

        with transaction.atomic():
            # Re-read under a row lock to serialise concurrent appends and
            # prevent a lost-update when two writers hit the same document.
            doc = Document.objects.select_for_update().get(pk=doc.pk)
            time_str = data.get("time") or timezone.now().strftime("%H:%M")
            entry_block = f"\n\n### {time_str}{format_author_suffix(request.user.display_name)}\n{content}\n"
            doc.markdown = (doc.markdown or "").rstrip() + entry_block
            # Merge under the SAME lock as the text: the receipt is re-read from
            # the locked row, so a concurrent append's receipt is folded in
            # rather than clobbered by this request's stale copy.
            doc.pii_receipts = merge_field_receipt(
                doc.pii_receipts,
                "markdown",
                authored.receipt,
                stored_text=doc.markdown,
            )
            doc.save(update_fields=["markdown", "pii_receipts", "updated_at"])

        return Response(DocumentSerializer(doc, context={"tenant": tenant}).data, status=status.HTTP_201_CREATED)


class TodayView(APIView):
    """GET /api/v1/journal/today/ — convenience for today's daily note."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant(request.user)
        today = today_in_tenant_tz(tenant)
        doc = _get_or_create_document(tenant, "daily", str(today))
        return Response(DocumentSerializer(doc, context={"tenant": tenant}).data)


class SidebarTreeView(APIView):
    """GET /api/v1/journal/tree/ — returns the sidebar tree structure."""

    permission_classes = [IsAuthenticated]

    @tenant_cache(ttl=120, tag="sidebar")
    def get(self, request):
        # Titles are stored in PII placeholder space (a project titled after a
        # redacted name reads "[PERSON_1]" until rehydrated); this is the
        # owner-facing sidebar, so rehydrate them for display.
        from apps.pii.authoring import resolve_receipt_values
        from apps.pii.redactor import rehydrate_for_tenant

        tenant = _get_tenant(request.user)
        entity_map = getattr(tenant, "pii_entity_map", None)
        today = str(today_in_tenant_tz(tenant))
        documents = Document.objects.filter(tenant=tenant).values("kind", "slug", "title", "pii_receipts", "updated_at")

        # Group by kind
        tree: dict[str, list] = defaultdict(list)
        for doc in documents:
            # Hide future daily notes from sidebar
            if doc["kind"] == "daily" and doc["slug"] > today:
                continue
            receipts = resolve_receipt_values(doc["pii_receipts"] or {}, entity_map)
            tree[doc["kind"]].append(
                {
                    "slug": doc["slug"],
                    "title": rehydrate_for_tenant(tenant, doc["title"]),
                    # Title only — the sidebar never renders the body, and a
                    # markdown receipt here would ship a whole document's
                    # placeholder list on every sidebar load.
                    "pii_receipts": {field: r for field, r in receipts.items() if field == "title"},
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
