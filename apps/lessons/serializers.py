from __future__ import annotations

from rest_framework import serializers

from .models import Lesson, LessonConnection


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
            "text",
            "context",
            "source_type",
            "source_ref",
            "tags",
        ]


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
            "tags",
            "cluster_id",
            "cluster_label",
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

    from_id = serializers.IntegerField(source="from_lesson_id")
    to_id = serializers.IntegerField(source="to_lesson_id")

    class Meta:
        model = LessonConnection
        fields = ["from_id", "to_id", "similarity", "connection_type"]
