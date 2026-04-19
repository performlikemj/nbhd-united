"""Dashboard API — aggregated views for the frontend."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import UsageRecord
from apps.integrations.models import Integration
from apps.journal.models import Document, JournalEntry, PendingExtraction, WeeklyReview
from apps.orchestrator.services import check_tenant_health
from apps.tenants.models import Tenant

_WEEKLY_SLUG_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _clean_markdown_preview(markdown: str, max_chars: int = 180) -> str:
    """Produce a clean, prose-only preview snippet from markdown.

    Drops heading lines entirely (they become noise in a short preview),
    strips formatting markers, and collapses whitespace. The output is
    plain text suitable for display on a compact dashboard card.
    """
    if not markdown:
        return ""
    text = markdown
    # Drop heading lines entirely — the first heading typically restates the title
    text = re.sub(r"^\s*#{1,6}\s+.*$", "", text, flags=re.MULTILINE)
    # Drop horizontal rules
    text = re.sub(r"^\s*[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # Strip bold/italic markers
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    # Markdown links → just the visible text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Inline code
    text = re.sub(r"`(.+?)`", r"\1", text)
    # List markers (unordered + ordered)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    # Blockquote markers
    text = re.sub(r"^\s*>\s*", "", text, flags=re.MULTILINE)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip(" ,;:-") + "\u2026"
    return text


def _derive_week_bounds(slug: str, fallback: date) -> tuple[date, date]:
    """Given a weekly document slug (YYYY-MM-DD, Monday) return (week_start, week_end).

    Falls back to the Monday/Sunday surrounding `fallback` if the slug isn't a
    parseable date (older slugs, manual entries, etc.).
    """
    match = _WEEKLY_SLUG_RE.match(slug or "")
    if match:
        try:
            start = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            return start, start + timedelta(days=6)
        except ValueError:
            pass
    monday = fallback - timedelta(days=fallback.weekday())
    return monday, monday + timedelta(days=6)


def _serialize_weekly_document(wd: dict) -> dict:
    """Shape a Document(kind=weekly) row for the Horizons Weekly Pulse fallback."""
    updated_at = wd["updated_at"]
    fallback_date = updated_at.date() if isinstance(updated_at, datetime) else updated_at
    week_start, week_end = _derive_week_bounds(wd.get("slug") or "", fallback_date)
    markdown = wd.get("markdown") or ""
    return {
        "id": str(wd["id"]),
        "title": wd.get("title") or "Weekly Review",
        "slug": wd.get("slug") or "",
        "week_start": str(week_start),
        "week_end": str(week_end),
        "preview": _clean_markdown_preview(markdown),
        "markdown": markdown,
        "updated_at": updated_at.isoformat(),
    }


class DashboardView(APIView):
    """Main dashboard — tenant status, usage, connections."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response(
                {"detail": "No tenant found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Usage stats
        usage = UsageRecord.objects.filter(tenant=tenant).aggregate(
            total_input_tokens=Sum("input_tokens"),
            total_output_tokens=Sum("output_tokens"),
            total_cost=Sum("cost_estimate"),
        )

        # Connected services
        connections = list(
            Integration.objects.filter(
                tenant=tenant,
                status=Integration.Status.ACTIVE,
            ).values("provider", "provider_email", "connected_at")
        )

        # Health check
        health = check_tenant_health(str(tenant.id))

        return Response(
            {
                "tenant": {
                    "id": str(tenant.id),
                    "status": tenant.status,
                    "model_tier": tenant.model_tier,
                    "provisioned_at": tenant.provisioned_at,
                },
                "usage": {
                    "messages_today": tenant.messages_today,
                    "messages_this_month": tenant.messages_this_month,
                    "tokens_this_month": tenant.tokens_this_month,
                    "estimated_cost_this_month": str(tenant.estimated_cost_this_month),
                    "monthly_token_budget": tenant.effective_token_budget,
                    "total_input_tokens": usage["total_input_tokens"] or 0,
                    "total_output_tokens": usage["total_output_tokens"] or 0,
                    "total_cost": str(usage["total_cost"] or 0),
                },
                "connections": connections,
                "health": health,
            }
        )


class UsageHistoryView(APIView):
    """Usage history — recent usage records."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        records = UsageRecord.objects.filter(tenant=tenant).order_by("-created_at")[:50]
        data = [
            {
                "id": str(r.id),
                "event_type": r.event_type,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "model_used": r.model_used,
                "cost_estimate": str(r.cost_estimate),
                "created_at": r.created_at,
            }
            for r in records
        ]
        return Response({"results": data})


class HorizonsView(APIView):
    """Horizons — goals, momentum, and weekly pulse for the frontend."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        today = timezone.now().date()
        thirty_days_ago = today - timedelta(days=29)

        # 1. Active goal documents
        goals = list(
            Document.objects.filter(
                tenant=tenant,
                kind=Document.Kind.GOAL,
            )
            .order_by("-updated_at")[:20]
            .values(
                "id",
                "title",
                "slug",
                "markdown",
                "created_at",
                "updated_at",
            )
        )

        # 2. Pending goal/task extractions (exclude expired)
        pending = list(
            PendingExtraction.objects.filter(
                tenant=tenant,
                kind__in=[PendingExtraction.Kind.GOAL, PendingExtraction.Kind.TASK],
                status=PendingExtraction.Status.PENDING,
                expires_at__gte=timezone.now(),
            )
            .order_by("-created_at")[:10]
            .values(
                "id",
                "kind",
                "text",
                "confidence",
                "source_date",
                "created_at",
            )
        )

        # 3. Weekly pulse (last 4 weeks) — try legacy model first
        weeks = list(
            WeeklyReview.objects.filter(
                tenant=tenant,
            )
            .order_by("-week_start")[:4]
            .values(
                "week_start",
                "week_end",
                "week_rating",
                "top_wins",
            )
        )

        # 3b. Also fetch Document(kind='weekly') as fallback
        weekly_docs = list(
            Document.objects.filter(
                tenant=tenant,
                kind=Document.Kind.WEEKLY,
            )
            .order_by("-updated_at")[:4]
            .values(
                "id",
                "title",
                "slug",
                "markdown",
                "updated_at",
            )
        )

        # 4. Mood trend (30 days)
        moods = list(
            JournalEntry.objects.filter(
                tenant=tenant,
                date__gte=thirty_days_ago,
            )
            .order_by("date")
            .values("date", "mood", "energy")
        )

        # 5. Momentum (30 days) — message counts + journal dates
        message_counts = dict(
            UsageRecord.objects.filter(
                tenant=tenant,
                created_at__date__gte=thirty_days_ago,
            )
            .values_list("created_at__date")
            .annotate(count=Count("id"))
        )
        journal_dates = set(
            JournalEntry.objects.filter(
                tenant=tenant,
                date__gte=thirty_days_ago,
            ).values_list("date", flat=True)
        )
        doc_dates = set(
            Document.objects.filter(
                tenant=tenant,
                kind=Document.Kind.DAILY,
                created_at__date__gte=thirty_days_ago,
            ).values_list("created_at__date", flat=True)
        )
        all_journal_dates = journal_dates | doc_dates

        momentum = []
        for i in range(30):
            d = today - timedelta(days=29 - i)
            mc = message_counts.get(d, 0)
            hj = d in all_journal_dates
            momentum.append({"date": str(d), "message_count": mc, "has_journal": hj})

        # Current streak (consecutive days with activity, from today backwards)
        streak = 0
        for i in range(29, -1, -1):
            day = momentum[i]
            if day["message_count"] > 0 or day["has_journal"]:
                streak += 1
            else:
                break

        return Response(
            {
                "goals": [
                    {
                        "id": str(g["id"]),
                        "title": g["title"] or "Untitled Goal",
                        "slug": g["slug"],
                        "preview": (g["markdown"] or "")[:200],
                        "created_at": g["created_at"].isoformat(),
                        "updated_at": g["updated_at"].isoformat(),
                    }
                    for g in goals
                ],
                "pending_extractions": [
                    {
                        "id": str(p["id"]),
                        "kind": p["kind"],
                        "text": p["text"],
                        "confidence": p["confidence"],
                        "source_date": str(p["source_date"]) if p["source_date"] else None,
                        "created_at": p["created_at"].isoformat(),
                    }
                    for p in pending
                ],
                "weekly_pulse": [
                    {
                        "week_start": str(w["week_start"]),
                        "week_end": str(w["week_end"]),
                        "week_rating": w["week_rating"],
                        "top_win": w["top_wins"][0] if w["top_wins"] else None,
                    }
                    for w in weeks
                ],
                "weekly_documents": [_serialize_weekly_document(wd) for wd in weekly_docs],
                "mood_trend": [{"date": str(m["date"]), "mood": m["mood"], "energy": m["energy"]} for m in moods],
                "momentum": momentum,
                "current_streak": streak,
            }
        )
