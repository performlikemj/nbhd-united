"""Serializers for journal artifacts."""
from __future__ import annotations

from rest_framework import serializers

from .models import JournalEntry, WeeklyReview

MAX_LIST_ITEMS = 10


def _validate_string_list(*, value, field_name: str, allow_empty: bool = True) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise serializers.ValidationError(f"{field_name} must be an array of strings.")
    if len(value) > MAX_LIST_ITEMS:
        raise serializers.ValidationError(f"{field_name} cannot contain more than {MAX_LIST_ITEMS} items.")

    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise serializers.ValidationError(f"{field_name} must contain only strings.")
        text = item.strip()
        if not text:
            raise serializers.ValidationError(f"{field_name} cannot contain empty strings.")
        cleaned.append(text)

    if not allow_empty and not cleaned:
        raise serializers.ValidationError(f"{field_name} must include at least one item.")
    return cleaned


class JournalEntryRuntimeSerializer(serializers.ModelSerializer):
    class Meta:
        model = JournalEntry
        fields = (
            "id",
            "tenant",
            "date",
            "mood",
            "energy",
            "wins",
            "challenges",
            "reflection",
            "raw_text",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "tenant", "created_at", "updated_at")

    def validate_wins(self, value):
        return _validate_string_list(field_name="wins", value=value)

    def validate_challenges(self, value):
        return _validate_string_list(field_name="challenges", value=value)

    def validate_mood(self, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise serializers.ValidationError("mood is required.")
        return normalized

    def validate_raw_text(self, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise serializers.ValidationError("raw_text is required.")
        return normalized

    def validate_reflection(self, value: str) -> str:
        return value.strip()

    def create(self, validated_data: dict) -> JournalEntry:
        tenant = self.context["tenant"]
        return JournalEntry.objects.create(tenant=tenant, **validated_data)


class WeeklyReviewRuntimeSerializer(serializers.ModelSerializer):
    class Meta:
        model = WeeklyReview
        fields = (
            "id",
            "tenant",
            "week_start",
            "week_end",
            "mood_summary",
            "top_wins",
            "top_challenges",
            "lessons",
            "week_rating",
            "intentions_next_week",
            "raw_text",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "tenant", "created_at", "updated_at")

    def validate(self, attrs: dict) -> dict:
        week_start = attrs.get("week_start")
        week_end = attrs.get("week_end")
        if week_start and week_end and week_start > week_end:
            raise serializers.ValidationError({"week_end": "week_end must be on or after week_start."})
        return attrs

    def validate_top_wins(self, value):
        return _validate_string_list(field_name="top_wins", value=value)

    def validate_top_challenges(self, value):
        return _validate_string_list(field_name="top_challenges", value=value)

    def validate_lessons(self, value):
        return _validate_string_list(field_name="lessons", value=value)

    def validate_intentions_next_week(self, value):
        return _validate_string_list(field_name="intentions_next_week", value=value)

    def validate_mood_summary(self, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise serializers.ValidationError("mood_summary is required.")
        return normalized

    def validate_raw_text(self, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise serializers.ValidationError("raw_text is required.")
        return normalized

    def create(self, validated_data: dict) -> WeeklyReview:
        tenant = self.context["tenant"]
        return WeeklyReview.objects.create(tenant=tenant, **validated_data)


def _build_raw_text(data: dict) -> str:
    parts = []
    if data.get("mood"):
        parts.append(f"Mood: {data['mood']}")
    if data.get("energy"):
        parts.append(f"Energy: {data['energy']}")
    if data.get("wins"):
        parts.append("Wins: " + ", ".join(data["wins"]))
    if data.get("challenges"):
        parts.append("Challenges: " + ", ".join(data["challenges"]))
    if data.get("reflection"):
        parts.append(f"Reflection: {data['reflection']}")
    return "\n".join(parts)


class JournalEntrySerializer(serializers.ModelSerializer):
    """User-facing serializer (JWT auth). Excludes tenant and auto-generates raw_text."""

    class Meta:
        model = JournalEntry
        fields = (
            "id",
            "date",
            "mood",
            "energy",
            "wins",
            "challenges",
            "reflection",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_wins(self, value):
        return _validate_string_list(field_name="wins", value=value)

    def validate_challenges(self, value):
        return _validate_string_list(field_name="challenges", value=value)

    def validate_mood(self, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise serializers.ValidationError("mood is required.")
        return normalized

    def validate_reflection(self, value: str) -> str:
        return value.strip()

    def create(self, validated_data: dict) -> JournalEntry:
        tenant = self.context["tenant"]
        raw_text = _build_raw_text(validated_data)
        return JournalEntry.objects.create(tenant=tenant, raw_text=raw_text, **validated_data)

    def update(self, instance: JournalEntry, validated_data: dict) -> JournalEntry:
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.raw_text = _build_raw_text(
            {f: getattr(instance, f) for f in ("mood", "energy", "wins", "challenges", "reflection")}
        )
        instance.save()
        return instance
