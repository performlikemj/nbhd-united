"""Serializer for the North Star (Purpose) layer.

Purpose is the direction *above* goals — see ``apps.journal.models.Purpose``.
Kept in its own module (not folded into ``lifecycle_serializers``) so the
North Star feature is a cohesive, self-contained slice.

``origin`` is intentionally read-only: whether a purpose came from the user or
was proposed by the assistant is set by the view (console → ``user_created``,
runtime propose → ``assistant_proposed``), never by request body — otherwise a
runtime caller could mint a ``user_created`` purpose and bypass the
consent-first proposal path.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.insights.pillars import Pillar

from .models import Purpose

_VALID_PILLARS = {p.value for p in Pillar}


class PurposeSerializer(serializers.ModelSerializer):
    """Read/write shape for a Purpose row."""

    class Meta:
        model = Purpose
        fields = [
            "id",
            "statement",
            "pillars",
            "status",
            "origin",
            "evidence",
            "pii_receipts",
            "confirmed_at",
            "retired_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "origin",
            "pii_receipts",
            "confirmed_at",
            "retired_at",
            "created_at",
            "updated_at",
        ]

    def validate_pillars(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("pillars must be a list of pillar slugs.")
        unknown = [p for p in value if p not in _VALID_PILLARS]
        if unknown:
            raise serializers.ValidationError(
                f"unknown pillar slug(s): {unknown!r}; allowed: {sorted(_VALID_PILLARS)!r}"
            )
        return value

    def validate_evidence(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("evidence must be a list of {kind, ref, note} objects.")
        return value

    def validate_statement(self, value):
        cleaned = (value or "").strip()
        if not cleaned:
            raise serializers.ValidationError("statement cannot be empty.")
        return cleaned

    def to_representation(self, instance):
        data = super().to_representation(instance)
        tenant = self.context.get("tenant")
        if tenant is None or not self.context.get("rehydrate"):
            data.pop("pii_receipts", None)
            return data
        from .store_authoring import owner_store_representation

        return owner_store_representation(instance, tenant, data, model_label="journal.Purpose")
