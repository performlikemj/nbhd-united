"""User-facing Document API views (Journal v2)."""
from __future__ import annotations

import datetime
from collections import defaultdict

from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import Tenant

from .document_serializers import (
    DocumentAppendSerializer,
    DocumentCreateSerializer,
    DocumentListSerializer,
    DocumentSerializer,
)
from .models import Document
from .templates_md import (
    DAILY_NOTE_TEMPLATE,
    daily_note_context,
    get_default_template,
    render_template,
)


def _get_tenant(user) -> Tenant:
    try:
        return user.tenant
    except Tenant.DoesNotExist as exc:
        raise Http404("Tenant not found.") from exc


def _get_or_create_document(tenant: Tenant, kind: str, slug: str) -> Document:
    """Get or create a document, applying default template for new docs."""
    doc, created = Document.objects.get_or_create(
        tenant=tenant,
        kind=kind,
        slug=slug,
        defaults={
            "title": _default_title(kind, slug),
            "markdown": _default_markdown(kind, slug),
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


def _default_markdown(kind: str, slug: str) -> str:
    """Generate default markdown content for a new document."""
    template = get_default_template(kind)
    if not template:
        return ""

    if kind == "daily":
        try:
            d = datetime.date.fromisoformat(slug)
            return render_template(template, daily_note_context(d))
        except ValueError:
            return template

    context = {"date": slug, "title": _default_title(kind, slug)}
    return render_template(template, context)


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
                "markdown": data.get("markdown") or _default_markdown(data["kind"], data["slug"]),
            },
        )

        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(DocumentSerializer(doc).data, status=status_code)


class DocumentDetailView(APIView):
    """GET/PATCH/DELETE /api/v1/journal/documents/<kind>/<slug>/"""

    permission_classes = [IsAuthenticated]

    def get(self, request, kind: str, slug: str):
        tenant = _get_tenant(request.user)
        doc = _get_or_create_document(tenant, kind, slug)
        return Response(DocumentSerializer(doc).data)

    def patch(self, request, kind: str, slug: str):
        tenant = _get_tenant(request.user)
        doc = _get_or_create_document(tenant, kind, slug)

        markdown = request.data.get("markdown")
        title = request.data.get("title")

        if markdown is not None:
            doc.markdown = markdown
        if title is not None:
            doc.title = title

        doc.save()
        return Response(DocumentSerializer(doc).data)

    def delete(self, request, kind: str, slug: str):
        tenant = _get_tenant(request.user)
        try:
            doc = Document.objects.get(tenant=tenant, kind=kind, slug=slug)
        except Document.DoesNotExist:
            raise Http404("Document not found.")
        doc.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


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

        time_str = data.get("time") or timezone.now().strftime("%H:%M")
        entry_block = f"\n\n### {time_str} — MJ\n{data['content'].strip()}\n"

        doc.markdown = (doc.markdown or "").rstrip() + entry_block
        doc.save()

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

    def get(self, request):
        tenant = _get_tenant(request.user)
        documents = Document.objects.filter(tenant=tenant).values("kind", "slug", "title", "updated_at")

        # Group by kind
        tree: dict[str, list] = defaultdict(list)
        for doc in documents:
            tree[doc["kind"]].append({
                "slug": doc["slug"],
                "title": doc["title"],
                "updated_at": doc["updated_at"].isoformat() if doc["updated_at"] else None,
            })

        # Sort daily notes by slug (date) descending
        if "daily" in tree:
            tree["daily"].sort(key=lambda x: x["slug"], reverse=True)

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
