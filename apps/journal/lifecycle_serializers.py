"""Serializers for the typed Goal/Task lifecycle (PR feat/journal-typed-lifecycle)."""

from __future__ import annotations

from rest_framework import serializers

from .models import Goal, Task


class GoalSerializer(serializers.ModelSerializer):
    """Read/write shape for Goal lifecycle.

    Note: ``migrated_from_document`` is intentionally excluded — it's an
    internal audit pointer that should never be exposed to the agent or UI.
    """

    parent_goal_id = serializers.PrimaryKeyRelatedField(
        source="parent_goal",
        queryset=Goal.objects.all(),
        required=False,
        allow_null=True,
    )
    topic_id = serializers.PrimaryKeyRelatedField(
        source="topic",
        queryset=None,  # populated lazily — see __init__
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Goal
        fields = [
            "id",
            "title",
            "description",
            "pillar",
            "topic_id",
            "target",
            "status",
            "parent_goal_id",
            "target_date",
            "achieved_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "achieved_at", "created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Avoid importing insights at module import time (Django app loading order).
        from apps.insights.models import TopicRegistry

        self.fields["topic_id"].queryset = TopicRegistry.objects.all()

    def validate(self, attrs):
        # Scope parent_goal to the same tenant — protects against cross-tenant linkage.
        tenant = self.context.get("tenant")
        parent = attrs.get("parent_goal")
        if tenant is not None and parent is not None and parent.tenant_id != tenant.id:
            raise serializers.ValidationError({"parent_goal_id": "Parent goal must belong to the same tenant."})
        return attrs

    def create(self, validated_data):
        tenant = self.context["tenant"]
        return Goal.objects.create(tenant=tenant, **validated_data)


class TaskSerializer(serializers.ModelSerializer):
    """Read/write shape for Task lifecycle."""

    parent_goal_id = serializers.PrimaryKeyRelatedField(
        source="parent_goal",
        queryset=Goal.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Task
        fields = [
            "id",
            "title",
            "description",
            "pillar",
            "status",
            "due_date",
            "completed_at",
            "parent_goal_id",
            "related_ref",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "completed_at", "created_at", "updated_at"]

    def validate(self, attrs):
        tenant = self.context.get("tenant")
        parent = attrs.get("parent_goal")
        if tenant is not None and parent is not None and parent.tenant_id != tenant.id:
            raise serializers.ValidationError({"parent_goal_id": "Parent goal must belong to the same tenant."})
        return attrs

    def create(self, validated_data):
        tenant = self.context["tenant"]
        return Task.objects.create(tenant=tenant, **validated_data)
