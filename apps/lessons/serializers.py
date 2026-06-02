"""Serializers for lessons — CRUD, constellation, galaxy, tutoring, and star journaling."""

from __future__ import annotations

from rest_framework import serializers

from .models import Lesson, LessonConnection, StarJournalEntry

# ── Base CRUD serializers (existing, preserved) ────────────────


class LessonSerializer(serializers.ModelSerializer):
    """Full lesson representation for API responses."""

    class Meta:
        model = Lesson
        exclude = ["embedding"]


class LessonCreateSerializer(serializers.ModelSerializer):
    """Serializer for lesson creation."""

    class Meta:
        model = Lesson
        fields = [
            "id",
            "text",
            "context",
            "source_type",
            "source_ref",
            "tags",
        ]
        read_only_fields = ["id"]


class LessonApprovalSerializer(serializers.Serializer):
    """Serializer for approve/dismiss state transitions."""

    status = serializers.ChoiceField(choices=["approved", "dismissed"], required=False)


class ConstellationNodeSerializer(serializers.ModelSerializer):
    """Node representation used by constellation visualizations."""

    x = serializers.SerializerMethodField()
    y = serializers.SerializerMethodField()

    class Meta:
        model = Lesson
        fields = [
            "id",
            "text",
            "context",
            "tags",
            "cluster_id",
            "cluster_label",
            "source_type",
            "source_ref",
            "x",
            "y",
            "created_at",
        ]

    def get_x(self, obj):
        return getattr(obj, "position_x", None)

    def get_y(self, obj):
        return getattr(obj, "position_y", None)


class ConstellationEdgeSerializer(serializers.ModelSerializer):
    """Edge representation for lesson links."""

    source = serializers.IntegerField(source="from_lesson_id")
    target = serializers.IntegerField(source="to_lesson_id")

    class Meta:
        model = LessonConnection
        fields = ["source", "target", "similarity", "connection_type"]


# ── Galaxy / Game serializers ─────────────────────────────────


class GalaxyStarSerializer(serializers.ModelSerializer):
    """Star representation for the galaxy map — lightweight with game state."""

    x = serializers.SerializerMethodField()
    y = serializers.SerializerMethodField()
    journal_count = serializers.SerializerMethodField()
    connection_count = serializers.SerializerMethodField()

    class Meta:
        model = Lesson
        fields = [
            "id",
            "text",
            "tags",
            "cluster_id",
            "cluster_label",
            "star_stage",
            "x",
            "y",
            "journal_count",
            "connection_count",
            "last_tutored_at",
            "last_visited_at",
            "galaxy_note",
            "source_type",
            "created_at",
        ]

    def get_x(self, obj):
        return getattr(obj, "position_x", None)

    def get_y(self, obj):
        return getattr(obj, "position_y", None)

    def get_journal_count(self, obj):
        return getattr(obj, "journal_entries", None) and obj.journal_entries.count() or 0

    def get_connection_count(self, obj):
        return getattr(obj, "connections_out", None) and obj.connections_out.count() or 0


class GalaxyEdgeSerializer(serializers.ModelSerializer):
    """Edge representation for galaxy connections."""

    source = serializers.IntegerField(source="from_lesson_id")
    target = serializers.IntegerField(source="to_lesson_id")

    class Meta:
        model = LessonConnection
        fields = ["source", "target", "similarity", "connection_type"]


class TutoringStartSerializer(serializers.Serializer):
    """Input for starting a tutoring session — nothing needed, star is in the URL."""

    pass


class TutoringMessageSerializer(serializers.Serializer):
    """Player message in an active tutoring session."""

    message = serializers.CharField(required=True, min_length=1, max_length=5000)
    action = serializers.ChoiceField(
        choices=["continue", "skip", "end"],
        default="continue",
    )


class TutoringStateSerializer(serializers.Serializer):
    """Current tutoring session state (read-only)."""

    session_id = serializers.CharField()
    star_id = serializers.IntegerField()
    star_text = serializers.CharField()
    current_phase = serializers.CharField()
    phase_index = serializers.IntegerField()
    total_phases = serializers.IntegerField()
    phases_completed = serializers.ListField(child=serializers.CharField())


class StarJournalEntrySerializer(serializers.ModelSerializer):
    """Serializes a star journal entry."""

    class Meta:
        model = StarJournalEntry
        fields = ["id", "star", "text", "entry_type", "tags", "created_at"]
        read_only_fields = ["id", "created_at"]


class StarJournalEntryCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating a star journal entry."""

    class Meta:
        model = StarJournalEntry
        fields = ["text", "entry_type", "tags"]


class StarNoteSerializer(serializers.Serializer):
    """Serializer for updating a star's galaxy_note."""

    note = serializers.CharField(required=True, max_length=1000)


class StarConnectSerializer(serializers.Serializer):
    """Serializer for manually connecting two stars."""

    target_star_id = serializers.IntegerField(required=True)
    connection_type = serializers.ChoiceField(
        choices=["similar", "builds_on", "contradicts", "user_linked"],
        default="user_linked",
    )


class StarDetailSerializer(serializers.ModelSerializer):
    """Full star detail for landing — includes journal preview."""

    x = serializers.SerializerMethodField()
    y = serializers.SerializerMethodField()
    journal_entries = serializers.SerializerMethodField()
    connection_count = serializers.SerializerMethodField()
    tutoring_sessions_count = serializers.SerializerMethodField()

    class Meta:
        model = Lesson
        fields = [
            "id",
            "text",
            "context",
            "tags",
            "cluster_id",
            "cluster_label",
            "source_type",
            "source_ref",
            "star_stage",
            "x",
            "y",
            "galaxy_note",
            "journal_entries",
            "connection_count",
            "tutoring_sessions_count",
            "last_tutored_at",
            "last_visited_at",
            "created_at",
            "approved_at",
        ]

    def get_x(self, obj):
        return getattr(obj, "position_x", None)

    def get_y(self, obj):
        return getattr(obj, "position_y", None)

    def get_journal_entries(self, obj):
        entries = obj.journal_entries.order_by("-created_at")[:5]
        return [
            {
                "id": str(e.id),
                "text": e.text[:200] + ("..." if len(e.text) > 200 else ""),
                "entry_type": e.entry_type,
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ]

    def get_connection_count(self, obj):
        return obj.connections_out.count()

    def get_tutoring_sessions_count(self, obj):
        return obj.tutoring_sessions_count


class GalaxySummarySerializer(serializers.Serializer):
    """Lightweight galaxy summary for the game HUD."""

    total_stars = serializers.IntegerField()
    proto_count = serializers.IntegerField()
    ignited_count = serializers.IntegerField()
    radiant_count = serializers.IntegerField()
    supernova_count = serializers.IntegerField()
    cluster_count = serializers.IntegerField()
    recent_activity = serializers.ListField(child=serializers.DictField())
