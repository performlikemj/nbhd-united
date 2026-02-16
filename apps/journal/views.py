"""User-facing Journal API views."""
from __future__ import annotations

import datetime
from uuid import UUID

from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import Tenant

from .md_utils import append_entry_markdown, parse_daily_note, serialise_daily_note
from .models import DailyNote, JournalEntry, UserMemory, WeeklyReview
from .serializers import (
    DailyNoteEntryInputSerializer,
    DailyNoteEntryPatchSerializer,
    JournalEntrySerializer,
    MemoryPatchSerializer,
    WeeklyReviewSerializer,
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
    """GET /api/v1/journal/daily/<date>/ — parsed structured entries."""

    permission_classes = [IsAuthenticated]

    def get(self, request, date: str):
        tenant = _get_tenant_for_user(request.user)
        try:
            d = datetime.date.fromisoformat(date)
        except ValueError:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

        note = DailyNote.objects.filter(tenant=tenant, date=d).first()
        entries = parse_daily_note(note.markdown) if note else []
        return Response({"date": date, "entries": entries})


class DailyNoteEntryListView(APIView):
    """POST /api/v1/journal/daily/<date>/entries/ — append an entry."""

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

        note, _ = DailyNote.objects.get_or_create(tenant=tenant, date=d)

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
        return Response({"date": date, "entries": entries}, status=status.HTTP_201_CREATED)


class DailyNoteEntryDetailView(APIView):
    """PATCH/DELETE /api/v1/journal/daily/<date>/entries/<index>/"""

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

        return Response({"date": date, "entries": entries})

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


# ---------------------------------------------------------------------------
# Long-Term Memory views (user-facing)
# ---------------------------------------------------------------------------


class MemoryView(APIView):
    """GET/PATCH /api/v1/journal/memory/"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant_for_user(request.user)
        memory = UserMemory.objects.filter(tenant=tenant).first()
        return Response({
            "markdown": memory.markdown if memory else "",
            "updated_at": memory.updated_at.isoformat() if memory else None,
        })

    def put(self, request):
        tenant = _get_tenant_for_user(request.user)
        serializer = MemoryPatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        memory, _ = UserMemory.objects.get_or_create(tenant=tenant)
        memory.markdown = serializer.validated_data["markdown"]
        memory.save()

        return Response({
            "markdown": memory.markdown,
            "updated_at": memory.updated_at.isoformat(),
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
        return Response(serializer.data)

    def post(self, request):
        tenant = _get_tenant_for_user(request.user)
        serializer = WeeklyReviewSerializer(data=request.data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class WeeklyReviewDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, review_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        review = _get_weekly_review_for_tenant(tenant=tenant, review_id=review_id)
        return Response(WeeklyReviewSerializer(review).data)

    def patch(self, request, review_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        review = _get_weekly_review_for_tenant(tenant=tenant, review_id=review_id)
        serializer = WeeklyReviewSerializer(review, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        return Response(WeeklyReviewSerializer(updated).data)

    def delete(self, request, review_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        review = _get_weekly_review_for_tenant(tenant=tenant, review_id=review_id)
        review.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
