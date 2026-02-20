from __future__ import annotations

from collections import Counter

from django.db.models import QuerySet
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Lesson, LessonConnection
from .serializers import (
    ConstellationEdgeSerializer,
    ConstellationNodeSerializer,
    LessonApprovalSerializer,
    LessonCreateSerializer,
    LessonSerializer,
)


class LessonViewSet(viewsets.ModelViewSet):
    """Tenant-scoped lesson CRUD and constellation helper actions."""

    permission_classes = [IsAuthenticated]
    queryset = Lesson.objects.none()
    serializer_class = LessonSerializer
    pagination_class = None

    def get_serializer_class(self):
        if self.action == "create":
            return LessonCreateSerializer
        if self.action in {"approve", "dismiss"}:
            return LessonApprovalSerializer
        return LessonSerializer

    def get_queryset(self) -> QuerySet[Lesson]:
        if not hasattr(self.request.user, "tenant"):
            return Lesson.objects.none()

        status_filter = self.request.query_params.get("status") if self.action == "list" else None
        qs = Lesson.objects.filter(tenant=self.request.user.tenant)

        if self.action == "list":
            qs = qs.filter(status=status_filter or "approved")
        elif self.action == "pending":
            qs = qs.filter(status="pending")

        return qs

    def perform_create(self, serializer: LessonCreateSerializer):
        serializer.save(tenant=self.request.user.tenant)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        serializer = LessonApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data.get("status") and serializer.validated_data["status"] != "approved":
            return Response(
                {"detail": "Use /dismiss/ to mark lesson as dismissed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lesson = self.get_object()
        lesson.status = "approved"
        lesson.approved_at = timezone.now()
        lesson.save(update_fields=["status", "approved_at"])

        # Process embedding and connections
        try:
            process_approved_lesson(lesson)
        except Exception:
            # Don't fail approval if embedding/connection processing breaks.
            pass

        # Re-cluster if enough lessons are approved
        try:
            from .clustering import refresh_constellation

            approved_count = Lesson.objects.filter(
                tenant=lesson.tenant,
                status="approved",
            ).count()
            if approved_count >= 5:
                refresh_constellation(lesson.tenant)
        except Exception:
            # Don't fail approval if clustering fails.
            pass

        return Response(LessonSerializer(lesson).data)

    @action(detail=True, methods=["post"], url_path="dismiss")
    def dismiss(self, request, pk=None):
        serializer = LessonApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data.get("status") and serializer.validated_data["status"] != "dismissed":
            return Response(
                {"detail": "Use /approve/ to mark lesson as approved."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lesson = self.get_object()
        lesson.status = "dismissed"
        lesson.approved_at = None
        lesson.save(update_fields=["status", "approved_at"])
        return Response(LessonSerializer(lesson).data)

    @action(detail=False, methods=["post"], url_path="refresh")
    def refresh(self, request):
        """Re-run clustering and position calculation for this tenant."""

        if not hasattr(request.user, "tenant"):
            return Response(
                {"error": "tenant_required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from .clustering import refresh_constellation

        result = refresh_constellation(request.user.tenant)
        return Response(result)

    @action(detail=False, methods=["get"], url_path="pending")
    def pending(self, request):
        lessons = self.get_queryset().filter(status="pending")
        serializer = LessonSerializer(lessons, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="constellation")
    def constellation(self, request):
        lessons = self.get_queryset().filter(status="approved")
        lesson_ids = [lesson.id for lesson in lessons]

        edges = LessonConnection.objects.filter(
            from_lesson_id__in=lesson_ids,
            to_lesson_id__in=lesson_ids,
        )

        return Response(
            {
                "nodes": ConstellationNodeSerializer(lessons, many=True).data,
                "edges": ConstellationEdgeSerializer(edges, many=True).data,
            }
        )

    @action(detail=False, methods=["get"], url_path="clusters")
    def clusters(self, request):
        lessons = self.get_queryset().filter(status="approved").exclude(cluster_id__isnull=True)
        grouped = {}

        for lesson in lessons:
            grouped.setdefault((lesson.cluster_id, lesson.cluster_label), []).append(lesson)

        clusters = []
        for (cluster_id, cluster_label), cluster_lessons in grouped.items():
            all_tags = [tag for lesson in cluster_lessons for tag in lesson.tags]
            common_tags = [tag for tag, _count in Counter(all_tags).most_common(3)]
            clusters.append(
                {
                    "cluster_id": cluster_id,
                    "cluster_label": cluster_label,
                    "count": len(cluster_lessons),
                    "top_tags": common_tags,
                }
            )

        return Response(clusters)

    @action(detail=False, methods=["get"], url_path="search")
    def search(self, request):
        query = (request.query_params.get("q") or "").strip()
        if not query:
            return Response({"detail": "Missing query parameter 'q'."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            limit = int(request.query_params.get("limit", 10))
            if limit <= 0:
                raise ValueError
        except ValueError:
            return Response({"detail": "Invalid 'limit' value."}, status=status.HTTP_400_BAD_REQUEST)

        lessons = search_lessons(tenant=request.user.tenant, query=query, limit=limit)
        payload = [
            {**LessonSerializer(lesson).data, "similarity": lesson.similarity}
            for lesson in lessons
        ]
        return Response(payload)
