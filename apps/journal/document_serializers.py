"""Serializers for the v2 Document model."""

from __future__ import annotations

from rest_framework import serializers

from .models import Document


class _RehydrateFieldMixin:
    """Rehydrate a stored placeholder-space field for the owner.

    Reads ``tenant`` from serializer context. A call site that forgot to pass
    it degrades to today's behaviour (raw stored text) rather than crashing —
    every owner-facing view in document_views.py passes context={"tenant": …}.
    """

    def _rehydrated(self, value: str) -> str:
        from apps.pii.redactor import rehydrate_for_tenant

        tenant = self.context.get("tenant")
        if tenant is None:
            return value
        return rehydrate_for_tenant(tenant, value)


class DocumentSerializer(_RehydrateFieldMixin, serializers.ModelSerializer):
    # ``markdown`` and ``title`` are stored in PII placeholder space (the
    # assistant writes them on redacted input, so they contain
    # ``[LOCATION_330]`` tokens). This is the owner-facing serve boundary, so
    # rehydrate placeholders back to real values here. The counterpart write
    # endpoints (DocumentDetailView.patch / DocumentAppendView.post) re-redact
    # incoming markdown so the round-tripped real value never lands back in
    # storage where the agent would read it.
    markdown = serializers.SerializerMethodField()
    title = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = ("id", "kind", "slug", "title", "markdown", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

    def get_markdown(self, obj) -> str:
        return self._rehydrated(obj.markdown)

    def get_title(self, obj) -> str:
        return self._rehydrated(obj.title)


class DocumentListSerializer(_RehydrateFieldMixin, serializers.ModelSerializer):
    """Lighter serializer for listing (no markdown body).

    ``title`` gets the same owner-facing rehydration as the detail serializer —
    agent-authored titles carry placeholders exactly like markdown bodies (the
    sidebar rehydrates the identical field).
    """

    title = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = ("id", "kind", "slug", "title", "updated_at")
        read_only_fields = ("id", "updated_at")

    def get_title(self, obj) -> str:
        return self._rehydrated(obj.title)


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
