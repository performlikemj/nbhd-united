"""Serializers for the v2 Document model."""
from __future__ import annotations

from rest_framework import serializers

from .models import Document


class DocumentSerializer(serializers.ModelSerializer):
    title = serializers.SerializerMethodField()
    markdown = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = ("id", "kind", "slug", "title", "markdown", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

    def get_title(self, obj: Document) -> str:
        return obj.title_plaintext

    def get_markdown(self, obj: Document) -> str:
        return obj.markdown_plaintext


class DocumentListSerializer(serializers.ModelSerializer):
    """Lighter serializer for listing (no markdown body)."""

    title = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = ("id", "kind", "slug", "title", "updated_at")
        read_only_fields = ("id", "updated_at")

    def get_title(self, obj: Document) -> str:
        return obj.title_plaintext


class DocumentAppendSerializer(serializers.Serializer):
    content = serializers.CharField()
    time = serializers.CharField(required=False, allow_blank=True, default="")


class DocumentCreateSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=Document.Kind.choices)
    slug = serializers.CharField(max_length=128)
    title = serializers.CharField(max_length=256)
    markdown = serializers.CharField(required=False, allow_blank=True, default="")


class SidebarTreeSerializer(serializers.Serializer):
    """Represents a tree node for the sidebar."""
    kind = serializers.CharField()
    slug = serializers.CharField()
    title = serializers.CharField()
    updated_at = serializers.DateTimeField(required=False)
