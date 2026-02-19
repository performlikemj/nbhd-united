"""User-facing Journal API views."""
from __future__ import annotations

import datetime
from datetime import timedelta
from uuid import UUID

from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import Tenant

from .md_utils import append_entry_markdown, parse_daily_note, serialise_daily_note
from .models import DailyNote, Document, JournalEntry, UserMemory, WeeklyReview
from .models import NoteTemplate
from .serializers import (
    DailyNoteEntryInputSerializer,
    DailyNoteEntryPatchSerializer,
    JournalEntrySerializer,
    NoteTemplateSerializer,
    MemoryPatchSerializer,
    DailyNoteTemplateSerializer,
    WeeklyReviewSerializer,
)
from .services import (
    set_daily_note_section,
    ensure_daily_note_template,
    get_or_seed_note_template,
    parse_daily_sections,
    set_daily_note_sections,
    upsert_default_daily_note,
)


def _get_tenant_for_user(user) -> Tenant:
    try:
        return user.tenant
    except Tenant.DoesNotExist as exc:
        raise Http404("Tenant not found.") from exc


def _get_entry_for_tenant(*, tenant: Tenant, entry_id: UUID) -> JournalEntry:
    try:
        return JournalEntry.objects.get(id=entry_id, tenant=tenant)
    except JournalEntry.DoesNotExist as exc:
        raise Http404("Journal entry not found.") from exc


def _get_weekly_review_for_tenant(*, tenant: Tenant, review_id: UUID) -> WeeklyReview:
    try:
        return WeeklyReview.objects.get(id=review_id, tenant=tenant)
    except WeeklyReview.DoesNotExist as exc:
        raise Http404("Weekly review not found.") from exc


DEPRECATION_HEADERS = {
    "Deprecation": "true",
    "Warning": '299 - "This endpoint is kept for backward compatibility only."',
}


def _note_template_response(note: DailyNote, *, include_entries: bool = False) -> dict:
    template, sections = get_or_seed_note_template(
        tenant=note.tenant,
        date_value=note.date,
        markdown=note.markdown,
    )
    payload = {
        "date": str(note.date),
        "markdown": note.markdown,
        "template_id": str(template.id),
        "template_slug": template.slug,
        "template_name": template.name,
        "sections": sections,
    }
    if include_entries:
        payload["entries"] = parse_daily_note(note.markdown)
    return payload


# ---------------------------------------------------------------------------
# Legacy JournalEntry views (untouched)
# ---------------------------------------------------------------------------


class JournalEntryListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant_for_user(request.user)
        queryset = JournalEntry.objects.filter(tenant=tenant).order_by("-date", "-created_at")

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            try:
                queryset = queryset.filter(date__gte=datetime.date.fromisoformat(date_from))
            except ValueError:
                pass
        if date_to:
            try:
                queryset = queryset.filter(date__lte=datetime.date.fromisoformat(date_to))
            except ValueError:
                pass

        serializer = JournalEntrySerializer(queryset, many=True)
        return Response(serializer.data)

    def post(self, request):
        tenant = _get_tenant_for_user(request.user)
        serializer = JournalEntrySerializer(data=request.data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class JournalEntryDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, entry_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        entry = _get_entry_for_tenant(tenant=tenant, entry_id=entry_id)
        return Response(JournalEntrySerializer(entry).data)

    def patch(self, request, entry_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        entry = _get_entry_for_tenant(tenant=tenant, entry_id=entry_id)
        serializer = JournalEntrySerializer(entry, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        return Response(JournalEntrySerializer(updated).data)

    def delete(self, request, entry_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        entry = _get_entry_for_tenant(tenant=tenant, entry_id=entry_id)
        entry.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Daily Note views (markdown-first)
# ---------------------------------------------------------------------------


class DailyNoteView(APIView):
    """GET /api/v1/journal/daily/<date>/ — sectionized + legacy entries."""

    permission_classes = [IsAuthenticated]

    def get(self, request, date: str):
        tenant = _get_tenant_for_user(request.user)
        try:
            d = datetime.date.fromisoformat(date)
        except ValueError:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        note = upsert_default_daily_note(tenant=tenant, note_date=d)

        return Response(_note_template_response(note))


class DailyNoteEntryListView(APIView):
    """POST /api/v1/journal/daily/<date>/entries/ — append an entry.

    .. deprecated:: Use DailyNoteSectionView or log-append instead.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, date: str):
        tenant = _get_tenant_for_user(request.user)
        try:
            d = datetime.date.fromisoformat(date)
        except ValueError:
            return Response({"error": "Invalid date format."}, status=400)

        serializer = DailyNoteEntryInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        note = upsert_default_daily_note(tenant=tenant, note_date=d)

        time_str = data.get("time") or timezone.now().strftime("%H:%M")
        note.markdown = append_entry_markdown(
            note.markdown,
            time=time_str,
            author="human",
            content=data["content"],
            mood=data.get("mood") or None,
            energy=data.get("energy"),
            date_str=str(d),
        )
        note.save()

        entries = parse_daily_note(note.markdown)
        return Response(
            {"date": date, "entries": entries},
            status=status.HTTP_201_CREATED,
            headers=DEPRECATION_HEADERS,
        )


class DailyNoteEntryDetailView(APIView):
    """PATCH/DELETE /api/v1/journal/daily/<date>/entries/<index>/

    .. deprecated:: Use DailyNoteSectionView instead.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request, date: str, index: int):
        tenant = _get_tenant_for_user(request.user)
        try:
            d = datetime.date.fromisoformat(date)
        except ValueError:
            return Response({"error": "Invalid date format."}, status=400)

        note = DailyNote.objects.filter(tenant=tenant, date=d).first()
        if not note:
            raise Http404("Daily note not found.")

        entries = parse_daily_note(note.markdown)
        if index < 0 or index >= len(entries):
            return Response({"error": "Entry index out of range."}, status=404)

        serializer = DailyNoteEntryPatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        entry = entries[index]
        for field in ("content", "mood", "energy"):
            if field in serializer.validated_data:
                entry[field] = serializer.validated_data[field]

        note.markdown = serialise_daily_note(str(d), entries)
        note.save()

        return Response({"date": date, "entries": entries}, headers=DEPRECATION_HEADERS)

    def delete(self, request, date: str, index: int):
        tenant = _get_tenant_for_user(request.user)
        try:
            d = datetime.date.fromisoformat(date)
        except ValueError:
            return Response({"error": "Invalid date format."}, status=400)

        note = DailyNote.objects.filter(tenant=tenant, date=d).first()
        if not note:
            raise Http404("Daily note not found.")

        entries = parse_daily_note(note.markdown)
        if index < 0 or index >= len(entries):
            return Response({"error": "Entry index out of range."}, status=404)

        entries.pop(index)
        note.markdown = serialise_daily_note(str(d), entries)
        note.save()

        return Response({"date": date, "entries": entries}, headers=DEPRECATION_HEADERS)


class DailyNoteTemplateView(APIView):
    """GET/PUT sectionized daily note payload by template sections."""

    permission_classes = [IsAuthenticated]

    def get(self, request, date: str):
        tenant = _get_tenant_for_user(request.user)
        try:
            d = datetime.date.fromisoformat(date)
        except ValueError:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        note = upsert_default_daily_note(tenant=tenant, note_date=d)
        return Response(_note_template_response(note))

    def put(self, request, date: str):
        tenant = _get_tenant_for_user(request.user)
        try:
            d = datetime.date.fromisoformat(date)
        except ValueError:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        template_id = request.data.get("template_id")
        sections = request.data.get("sections")
        if not isinstance(sections, list):
            return Response({"error": "sections must be an array."}, status=400)

        note = upsert_default_daily_note(tenant=tenant, note_date=d)
        template = note.template
        if template_id:
            template = NoteTemplate.objects.filter(tenant=tenant, id=template_id).first()
            if template is None:
                return Response({"error": "template not found."}, status=404)

        try:
            serializer = DailyNoteTemplateSerializer(data={
                "date": str(d),
                "template_id": str(template.id) if template else None,
                "template_slug": template.slug if template else "",
                "template_name": template.name if template else "",
                "markdown": request.data.get("markdown", ""),
                "sections": sections,
            })
            serializer.is_valid(raise_exception=True)
        except Exception as exc:
            return Response({"error": "Invalid payload.", "detail": str(exc)}, status=400)

        section_payload = serializer.validated_data["sections"]
        set_daily_note_sections(note=note, sections=section_payload, template=template)
        note.refresh_from_db()
        return Response(_note_template_response(note, include_entries=False), status=200)

    def delete(self, request, date: str, index: int):
        tenant = _get_tenant_for_user(request.user)
        try:
            d = datetime.date.fromisoformat(date)
        except ValueError:
            return Response({"error": "Invalid date format."}, status=400)

        note = DailyNote.objects.filter(tenant=tenant, date=d).first()
        if not note:
            raise Http404("Daily note not found.")

        entries = parse_daily_note(note.markdown)
        if index < 0 or index >= len(entries):
            return Response({"error": "Entry index out of range."}, status=404)

        entries.pop(index)
        note.markdown = serialise_daily_note(str(d), entries)
        note.save()

        return Response({"date": date, "entries": entries})


class DailyNoteSectionView(APIView):
    """PATCH /api/v1/journal/daily/<date>/sections/<slug>/"""

    permission_classes = [IsAuthenticated]

    def patch(self, request, date: str, slug: str):
        tenant = _get_tenant_for_user(request.user)
        try:
            d = datetime.date.fromisoformat(date)
        except ValueError:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        content = request.data.get("content")
        if content is None:
            return Response({"error": "content is required."}, status=400)

        note = upsert_default_daily_note(tenant=tenant, note_date=d)
        try:
            note, _sections = set_daily_note_section(
                note=note, section_slug=slug, content=str(content),
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)

        return Response(_note_template_response(note, include_entries=False))


# ---------------------------------------------------------------------------
# Long-Term Memory views (user-facing)
# ---------------------------------------------------------------------------


class MemoryView(APIView):
    """GET/PUT /api/v1/journal/memory/ — backed by Document model."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant_for_user(request.user)
        doc = Document.objects.filter(tenant=tenant, kind="memory", slug="long-term").first()
        return Response({
            "markdown": doc.markdown_plaintext if doc else "",
            "updated_at": doc.updated_at.isoformat() if doc else None,
        })

    def put(self, request):
        tenant = _get_tenant_for_user(request.user)
        serializer = MemoryPatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        doc, _ = Document.objects.get_or_create(
            tenant=tenant,
            kind="memory",
            slug="long-term",
            defaults={"title": "Memory"},
        )
        doc.markdown = serializer.validated_data["markdown"]
        doc.save()

        return Response({
            "markdown": doc.markdown_plaintext,
            "updated_at": doc.updated_at.isoformat(),
        })


# ---------------------------------------------------------------------------
# Weekly Review views (user-facing)
# ---------------------------------------------------------------------------


class WeeklyReviewListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant_for_user(request.user)
        queryset = WeeklyReview.objects.filter(tenant=tenant).order_by("-week_start")
        serializer = WeeklyReviewSerializer(queryset, many=True)
        return Response(serializer.data, headers=DEPRECATION_HEADERS)

    def post(self, request):
        tenant = _get_tenant_for_user(request.user)
        serializer = WeeklyReviewSerializer(data=request.data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=DEPRECATION_HEADERS)


class WeeklyReviewDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, review_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        review = _get_weekly_review_for_tenant(tenant=tenant, review_id=review_id)
        return Response(WeeklyReviewSerializer(review).data, headers=DEPRECATION_HEADERS)

    def patch(self, request, review_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        review = _get_weekly_review_for_tenant(tenant=tenant, review_id=review_id)
        serializer = WeeklyReviewSerializer(review, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        return Response(WeeklyReviewSerializer(updated).data, headers=DEPRECATION_HEADERS)

    def delete(self, request, review_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        review = _get_weekly_review_for_tenant(tenant=tenant, review_id=review_id)
        review.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Template management
# ---------------------------------------------------------------------------


class TemplateListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant_for_user(request.user)
        templates = NoteTemplate.objects.filter(tenant=tenant).order_by("-is_default", "name")
        serializer = NoteTemplateSerializer(templates, many=True, context={"tenant": tenant})
        return Response(serializer.data)

    def post(self, request):
        tenant = _get_tenant_for_user(request.user)
        serializer = NoteTemplateSerializer(data=request.data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        template = serializer.save()
        return Response(NoteTemplateSerializer(template).data, status=status.HTTP_201_CREATED)


class TemplateDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, template_id: str):
        tenant = _get_tenant_for_user(request.user)
        template = NoteTemplate.objects.filter(tenant=tenant, id=template_id).first()
        if not template:
            return Response({"error": "template not found."}, status=404)
        serializer = NoteTemplateSerializer(template)
        return Response(serializer.data)

    def patch(self, request, template_id: str):
        tenant = _get_tenant_for_user(request.user)
        template = NoteTemplate.objects.filter(tenant=tenant, id=template_id).first()
        if not template:
            return Response({"error": "template not found."}, status=404)
        serializer = NoteTemplateSerializer(template, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        template = serializer.save()

        # Push updated skill config to the agent's container when the default template changes.
        if template.is_default:
            try:
                from apps.orchestrator.tasks import update_tenant_config_task
                update_tenant_config_task.delay(str(tenant.id))
            except Exception:
                pass  # Non-blocking; config update is best-effort.

        return Response(NoteTemplateSerializer(template).data)

    def delete(self, request, template_id: str):
        tenant = _get_tenant_for_user(request.user)
        template = NoteTemplate.objects.filter(tenant=tenant, id=template_id).first()
        if not template:
            return Response({"error": "template not found."}, status=404)
        if template.is_default:
            alternate = NoteTemplate.objects.filter(tenant=tenant).exclude(id=template.id).first()
            if not alternate:
                return Response({"error": "cannot delete the last template."}, status=400)
            alternate.is_default = True
            alternate.save(update_fields=["is_default"])

        template.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
