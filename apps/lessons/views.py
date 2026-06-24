from __future__ import annotations

from collections import Counter

from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Lesson, LessonConnection, StarJournalEntry, TutoringSession
from .serializers import (
    ConstellationEdgeSerializer,
    ConstellationNodeSerializer,
    GalaxyEdgeSerializer,
    GalaxyReflectSerializer,
    GalaxyStarSerializer,
    LessonApprovalSerializer,
    LessonCreateSerializer,
    LessonSerializer,
    StarConnectSerializer,
    StarDetailSerializer,
    StarJournalEntryCreateSerializer,
    StarJournalEntrySerializer,
    StarNoteSerializer,
    TutoringInsightSerializer,
    TutoringMessageSerializer,
)
from .services import process_approved_lesson, search_lessons
from .tutoring import continue_tutoring, end_tutoring, get_tutoring_state, start_tutoring


class LessonViewSet(viewsets.ModelViewSet):
    """Tenant-scoped lesson CRUD, galaxy, tutoring, and star journaling."""

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
        elif self.action == "galaxy":
            qs = qs.filter(status="approved")

        return qs

    def perform_create(self, serializer: LessonCreateSerializer):
        serializer.save(tenant=self.request.user.tenant)

    # ── Approval ────────────────────────────────────────────────

    @action(detail=True, methods=["post", "patch"], url_path="approve")
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

        try:
            process_approved_lesson(lesson)
        except Exception:
            pass

        try:
            from .clustering import refresh_constellation

            approved_count = Lesson.objects.filter(
                tenant=lesson.tenant,
                status="approved",
            ).count()
            if approved_count >= 5:
                refresh_constellation(lesson.tenant)
        except Exception:
            pass

        return Response(LessonSerializer(lesson).data)

    @action(detail=True, methods=["post", "patch"], url_path="dismiss")
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
        if not hasattr(request.user, "tenant"):
            return Response({"error": "tenant_required"}, status=status.HTTP_400_BAD_REQUEST)

        from .clustering import refresh_constellation

        result = refresh_constellation(request.user.tenant)
        return Response(result)

    @action(detail=False, methods=["get"], url_path="pending")
    def pending(self, request):
        lessons = self.get_queryset().filter(status="pending")
        serializer = LessonSerializer(lessons, many=True)
        return Response(serializer.data)

    # ── Web Constellation View (existing, preserved) ────────────

    @action(detail=False, methods=["get"], url_path="constellation")
    def constellation(self, request):
        lessons = list(self.get_queryset().filter(status="approved"))
        lesson_ids = [lesson.id for lesson in lessons]

        edges = LessonConnection.objects.filter(
            from_lesson_id__in=lesson_ids,
            to_lesson_id__in=lesson_ids,
        )

        affinity_edges = []
        lessons_with_embeddings = [l for l in lessons if l.embedding is not None]
        if 2 <= len(lessons_with_embeddings) <= 150:
            import numpy as np

            embs = np.array([l.embedding for l in lessons_with_embeddings], dtype=np.float64)
            norms = np.linalg.norm(embs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            normalized = embs / norms
            sim_matrix = normalized @ normalized.T

            strong_pairs = set()
            for edge in edges:
                strong_pairs.add((edge.from_lesson_id, edge.to_lesson_id))
                strong_pairs.add((edge.to_lesson_id, edge.from_lesson_id))

            min_sim = 0.1 if len(lessons_with_embeddings) <= 5 else 0.3

            for i in range(len(lessons_with_embeddings)):
                for j in range(i + 1, len(lessons_with_embeddings)):
                    sim = float(sim_matrix[i, j])
                    lid_i = lessons_with_embeddings[i].id
                    lid_j = lessons_with_embeddings[j].id
                    if sim >= min_sim and (lid_i, lid_j) not in strong_pairs:
                        affinity_edges.append(
                            {
                                "source": lid_i,
                                "target": lid_j,
                                "similarity": round(sim, 4),
                                "connection_type": "affinity",
                            }
                        )

        grouped: dict[tuple[int, str], list[Lesson]] = {}
        for lesson in lessons:
            if lesson.cluster_id is not None:
                grouped.setdefault((lesson.cluster_id, lesson.cluster_label or ""), []).append(lesson)

        clusters = []
        for (cluster_id, cluster_label), cluster_lessons in grouped.items():
            all_tags = [tag for lesson in cluster_lessons for tag in lesson.tags]
            common_tags = [tag for tag, _count in Counter(all_tags).most_common(3)]
            clusters.append(
                {
                    "id": cluster_id,
                    "label": cluster_label,
                    "count": len(cluster_lessons),
                    "tags": common_tags,
                }
            )

        return Response(
            {
                "nodes": ConstellationNodeSerializer(lessons, many=True).data,
                "edges": ConstellationEdgeSerializer(edges, many=True).data,
                "affinity_edges": affinity_edges,
                "clusters": clusters,
            }
        )

    # ── Galaxy (Game Client) ────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="galaxy")
    def galaxy(self, request):
        """Full galaxy state for the game client — stars with game state, plus
        connection edges (stored LessonConnections + computed embedding affinity)
        and cluster groupings, so the client can draw constellations and links."""
        # Annotate the two per-star counts onto the single list query. Reading
        # them via SerializerMethodField's obj.<rel>.count() fired 2 COUNT
        # queries PER star (and .count() ignores any prefetch cache), so a
        # galaxy of N stars was 2N round-trips — ~30s against the trans-Pacific
        # DB. distinct=True keeps the two joins from inflating each other's count.
        lessons = list(
            self.get_queryset()
            .filter(status="approved")
            .annotate(
                journal_count_anno=Count("journal_entries", distinct=True),
                connection_count_anno=Count("connections_out", distinct=True),
            )
        )
        lesson_ids = [lesson.id for lesson in lessons]

        edges = LessonConnection.objects.filter(
            from_lesson_id__in=lesson_ids,
            to_lesson_id__in=lesson_ids,
        )
        edge_data = list(GalaxyEdgeSerializer(edges, many=True).data)

        # Affinity edges (embedding cosine similarity) so related stars are linked
        # even without stored connections — mirrors the web constellation view.
        affinity_edges = []
        lessons_with_embeddings = [l for l in lessons if l.embedding is not None]
        if 2 <= len(lessons_with_embeddings) <= 150:
            import numpy as np

            embs = np.array([l.embedding for l in lessons_with_embeddings], dtype=np.float64)
            norms = np.linalg.norm(embs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            normalized = embs / norms
            sim_matrix = normalized @ normalized.T

            stored = set()
            for edge in edges:
                stored.add((edge.from_lesson_id, edge.to_lesson_id))
                stored.add((edge.to_lesson_id, edge.from_lesson_id))

            min_sim = 0.1 if len(lessons_with_embeddings) <= 5 else 0.3
            for i in range(len(lessons_with_embeddings)):
                for j in range(i + 1, len(lessons_with_embeddings)):
                    sim = float(sim_matrix[i, j])
                    lid_i = lessons_with_embeddings[i].id
                    lid_j = lessons_with_embeddings[j].id
                    if sim >= min_sim and (lid_i, lid_j) not in stored:
                        affinity_edges.append(
                            {
                                "source": lid_i,
                                "target": lid_j,
                                "similarity": round(sim, 4),
                                "connection_type": "affinity",
                            }
                        )

        # Cluster groupings (the "constellations").
        grouped: dict[tuple[int, str], list[Lesson]] = {}
        for lesson in lessons:
            if lesson.cluster_id is not None:
                grouped.setdefault((lesson.cluster_id, lesson.cluster_label or ""), []).append(lesson)
        clusters = []
        for (cluster_id, cluster_label), cluster_lessons in grouped.items():
            all_tags = [tag for lesson in cluster_lessons for tag in lesson.tags]
            common_tags = [tag for tag, _count in Counter(all_tags).most_common(3)]
            clusters.append(
                {"id": cluster_id, "label": cluster_label, "count": len(cluster_lessons), "tags": common_tags}
            )

        return Response(
            {
                "stars": GalaxyStarSerializer(lessons, many=True).data,
                "edges": edge_data + affinity_edges,
                "clusters": clusters,
            }
        )

    @action(detail=False, methods=["get"], url_path="galaxy/summary")
    def galaxy_summary(self, request):
        """Quick HUD summary: star counts by stage, cluster count, recent activity."""
        if not hasattr(request.user, "tenant"):
            return Response({"error": "tenant_required"}, status=status.HTTP_400_BAD_REQUEST)
        tenant = self.request.user.tenant
        approved = Lesson.objects.filter(tenant=tenant, status="approved")

        stage_counts = dict(
            approved.aggregate(
                proto=Count("pk", filter=Q(star_stage="proto")),
                ignited=Count("pk", filter=Q(star_stage="ignited")),
                radiant=Count("pk", filter=Q(star_stage="radiant")),
                supernova=Count("pk", filter=Q(star_stage="supernova")),
            )
        )

        cluster_count = approved.exclude(cluster_id__isnull=True).values("cluster_id").distinct().count()

        recent = approved.filter(last_visited_at__isnull=False).order_by("-last_visited_at")[:5]

        data = {
            "total_stars": approved.count(),
            "proto_count": stage_counts.get("proto", 0),
            "ignited_count": stage_counts.get("ignited", 0),
            "radiant_count": stage_counts.get("radiant", 0),
            "supernova_count": stage_counts.get("supernova", 0),
            "cluster_count": cluster_count,
            "recent_activity": [
                {
                    "id": s.id,
                    "text": s.text[:80],
                    "visited": s.last_visited_at.isoformat() if s.last_visited_at else None,
                }
                for s in recent
            ],
        }
        return Response(data)

    @action(detail=False, methods=["get"], url_path="galaxy/insights")
    def galaxy_insights(self, request):
        """Recent tutoring signals for the caller tenant's stars.

        Loop-closing read surface: a future OpenClaw ``nbhd_tutoring_insights``
        tool calls this so the assistant can reference what the game learned
        about the player (restated accurately, found edge cases, connections
        made, topic shifts, mastery) without re-reading transcripts.
        """
        if not hasattr(request.user, "tenant"):
            return Response({"error": "tenant_required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            limit = int(request.query_params.get("limit", 10))
            if limit <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return Response({"detail": "Invalid 'limit' value."}, status=status.HTTP_400_BAD_REQUEST)

        sessions = (
            TutoringSession.objects.filter(star__tenant=request.user.tenant)
            .select_related("star")
            .order_by("-created_at")[:limit]
        )

        return Response(TutoringInsightSerializer(sessions, many=True).data)

    # ── Co-pilot (Galaxy Reflect) ───────────────────────────────

    @action(detail=False, methods=["post"], url_path="galaxy/reflect")
    def galaxy_reflect(self, request):
        """A spatially-aware co-pilot line for the star just landed on.

        Backend computes the spatial *evidence* (idea-space neighbours, cluster
        state, recent path, where to point next); the LLM only *phrases* it into
        one warm line. Lesson text is redacted before egress and the line is
        rehydrated on the way back, so a ``[PERSON_N]`` token can never reach the
        panel. The line is cached, rate-limited, and always falls back to a
        deterministic warm line — the panel is never empty, even when the LLM is
        unavailable. See ``apps/lessons/copilot.py``.
        """
        if not hasattr(request.user, "tenant"):
            return Response({"error": "tenant_required"}, status=status.HTTP_400_BAD_REQUEST)

        from django.core.cache import cache

        from . import copilot

        tenant = request.user.tenant
        serializer = GalaxyReflectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        star_id = data["star_id"]
        recent_ids = data.get("recent_star_ids", [])
        ship = data.get("ship")
        mode = data.get("mode", "land")

        cache_key = copilot.reflect_cache_key(tenant.id, mode, star_id, ship)
        cached = cache.get(cache_key)
        if cached is not None:
            # Rehydrate the cached (redacted) line against the LIVE tenant map so a
            # later entity-map change can't serve a stale name.
            return Response(copilot.finalize_egress(tenant, cached))

        target = Lesson.objects.filter(id=star_id, tenant=tenant, status="approved").first()
        if target is None:
            return Response({"detail": "Star not found."}, status=status.HTTP_404_NOT_FOUND)

        stars = list(
            Lesson.objects.filter(tenant=tenant, status="approved").only(
                "id",
                "text",
                "cluster_id",
                "cluster_label",
                "star_stage",
                "position_x",
                "position_y",
                "last_visited_at",
                "last_tutored_at",
                "galaxy_note",
            )
        )
        star_ids = {s.id for s in stars}
        edges_by_star = copilot.load_edges_for(target.id, star_ids)

        allow_llm = copilot.allow_llm_for(tenant.id)
        result = copilot.reflect(
            tenant=tenant,
            target=target,
            stars=stars,
            edges_by_star=edges_by_star,
            recent_ids=recent_ids,
            mode=mode,
            allow_llm=allow_llm,
        )

        # Only cache real (LLM) lines — a fallback line should be retried next
        # land in case the model is reachable then. The cached copy keeps the
        # REDACTED line + ``_mints``; rehydration happens per-response below.
        if result.get("source") == "llm":
            cache.set(cache_key, result, timeout=copilot.REFLECT_TTL)

        return Response(copilot.finalize_egress(tenant, result))

    # ── Star Landing ────────────────────────────────────────────

    @action(detail=True, methods=["post"], url_path="land")
    def land(self, request, pk=None):
        """Land on a star — mark visited, return full detail for the landing screen."""
        star = self.get_object()
        if star.status != "approved":
            return Response(
                {"detail": "Only approved stars can be visited."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        star.last_visited_at = timezone.now()
        star.save(update_fields=["last_visited_at"])

        return Response(StarDetailSerializer(star).data)

    # ── Tutoring ────────────────────────────────────────────────

    @action(detail=True, methods=["post"], url_path="tutor/start")
    def tutor_start(self, request, pk=None):
        """Begin a tutoring session on this star."""
        star = self.get_object()
        if star.status != "approved":
            return Response(
                {"detail": "Only approved stars can be tutored."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = start_tutoring(star)
        if "error" in result:
            return Response(result, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(result)

    @action(detail=True, methods=["post"], url_path="tutor/message")
    def tutor_message(self, request, pk=None):
        """Send a player message in an active tutoring session."""
        serializer = TutoringMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        session_id = serializer.validated_data.get("session_id", request.data.get("session_id"))
        if not session_id:
            return Response(
                {"detail": "session_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        action = serializer.validated_data["action"]
        player_message = serializer.validated_data["message"]

        if action == "end":
            result = end_tutoring(session_id)
            return Response(result)

        if action == "skip":
            player_message = "skip"

        result = continue_tutoring(session_id, player_message)
        if "error" in result:
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        # Auto-end if mastery achieved
        if result.get("mastery_achieved"):
            close_result = end_tutoring(session_id)
            return Response({**result, "session_close": close_result})

        return Response(result)

    @action(detail=True, methods=["post"], url_path="tutor/end")
    def tutor_end(self, request, pk=None):
        """End a tutoring session and persist the record."""
        session_id = request.data.get("session_id")
        if not session_id:
            return Response(
                {"detail": "session_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = end_tutoring(session_id)
        if "error" in result:
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        return Response(result)

    @action(detail=True, methods=["get"], url_path="tutor/state")
    def tutor_state(self, request, pk=None):
        """Get current state of an active tutoring session."""
        session_id = request.query_params.get("session_id")
        if not session_id:
            return Response(
                {"detail": "session_id query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        state = get_tutoring_state(session_id)
        if state is None:
            return Response(
                {"detail": "Tutoring session not found or expired."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(state)

    # ── Star Journal ────────────────────────────────────────────

    @action(detail=True, methods=["get"], url_path="journal")
    def star_journal_list(self, request, pk=None):
        """List journal entries attached to this star."""
        star = self.get_object()
        entries = star.journal_entries.all()
        return Response(StarJournalEntrySerializer(entries, many=True).data)

    @action(detail=True, methods=["post"], url_path="journal/create")
    def star_journal_create(self, request, pk=None):
        """Create a journal entry attached to this star."""
        star = self.get_object()
        serializer = StarJournalEntryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        entry = StarJournalEntry.objects.create(
            tenant=self.request.user.tenant,
            star=star,
            text=serializer.validated_data["text"],
            entry_type=serializer.validated_data.get("entry_type", "free"),
            tags=serializer.validated_data.get("tags", []),
        )
        # A note is deliberate engagement — grow the star (monotonic). Return the
        # resulting stage so the game can promote the star's visual in place.
        from .growth import apply_star_growth

        new_stage = apply_star_growth(star)
        return Response(
            {**StarJournalEntrySerializer(entry).data, "star_stage": new_stage},
            status=status.HTTP_201_CREATED,
        )

    # ── Star Actions ────────────────────────────────────────────

    @action(detail=True, methods=["post", "patch"], url_path="pin-note")
    def pin_note(self, request, pk=None):
        """Update the galaxy_note (pinned note) on a star."""
        star = self.get_object()
        serializer = StarNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        star.galaxy_note = serializer.validated_data["note"]
        star.save(update_fields=["galaxy_note"])
        return Response(StarDetailSerializer(star).data)

    @action(detail=True, methods=["post"], url_path="connect")
    def connect(self, request, pk=None):
        """Player manually creates a connection to another star."""
        star = self.get_object()
        serializer = StarConnectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        target_id = serializer.validated_data["target_star_id"]
        connection_type = serializer.validated_data["connection_type"]

        try:
            target = Lesson.objects.get(
                id=target_id,
                tenant=self.request.user.tenant,
                status="approved",
            )
        except Lesson.DoesNotExist:
            return Response(
                {"detail": "Target star not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if target.id == star.id:
            return Response(
                {"detail": "Cannot connect a star to itself."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create bidirectional connection
        LessonConnection.objects.get_or_create(
            from_lesson=star,
            to_lesson=target,
            defaults={"similarity": 1.0, "connection_type": connection_type},
        )
        LessonConnection.objects.get_or_create(
            from_lesson=target,
            to_lesson=star,
            defaults={"similarity": 1.0, "connection_type": connection_type},
        )

        # A connection is deliberate engagement on both ends — grow both stars.
        from .growth import apply_star_growth

        source_stage = apply_star_growth(star)
        target_stage = apply_star_growth(target)

        return Response(
            {
                "source": star.id,
                "target": target.id,
                "connection_type": connection_type,
                "source_star_stage": source_stage,
                "target_star_stage": target_stage,
            }
        )

    # ── Clusters & Search ──────────────────────────────────────

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
        payload = [{**LessonSerializer(lesson).data, "similarity": lesson.similarity} for lesson in lessons]
        return Response(payload)
