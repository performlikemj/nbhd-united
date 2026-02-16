"""Serializers for journal artifacts."""
from __future__ import annotations

from rest_framework import serializers

from .models import DailyNote, JournalEntry, NoteTemplate, UserMemory, WeeklyReview
from .services import _validate_template_sections

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


# ---------------------------------------------------------------------------
# Legacy JournalEntry serializers (untouched)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Daily note serializers
# ---------------------------------------------------------------------------


class DailyNoteEntryInputSerializer(serializers.Serializer):
    """Accepts a simple entry from the frontend to append to a daily note."""

    content = serializers.CharField()
    mood = serializers.CharField(required=False, allow_blank=True, default="")
    energy = serializers.IntegerField(required=False, default=None, allow_null=True)
    time = serializers.CharField(required=False, allow_blank=True, default="")


class DailyNoteEntryPatchSerializer(serializers.Serializer):
    """Patch a single entry by index."""

    content = serializers.CharField(required=False)
    mood = serializers.CharField(required=False, allow_blank=True)
    energy = serializers.IntegerField(required=False, allow_null=True)


class DailyNoteSectionSerializer(serializers.Serializer):
    slug = serializers.CharField()
    title = serializers.CharField()
    content = serializers.CharField(allow_blank=True)


class DailyNoteTemplateSerializer(serializers.Serializer):
    date = serializers.DateField(read_only=True)
    template_id = serializers.UUIDField(required=False, allow_null=True)
    template_slug = serializers.CharField(required=False, allow_blank=True)
    template_name = serializers.CharField(required=False, allow_blank=True)
    markdown = serializers.CharField()
    sections = DailyNoteSectionSerializer(many=True)


class MemoryPatchSerializer(serializers.Serializer):
    """Patch memory — full markdown replacement or section-based."""

    markdown = serializers.CharField()


# ---------------------------------------------------------------------------
# User-facing WeeklyReview serializer
# ---------------------------------------------------------------------------


def _build_weekly_review_raw_text(data: dict) -> str:
    parts = []
    if data.get("mood_summary"):
        parts.append(f"Mood: {data['mood_summary']}")
    if data.get("week_rating"):
        parts.append(f"Rating: {data['week_rating']}")
    if data.get("top_wins"):
        parts.append("Top wins: " + ", ".join(data["top_wins"]))
    if data.get("top_challenges"):
        parts.append("Top challenges: " + ", ".join(data["top_challenges"]))
    if data.get("lessons"):
        parts.append("Lessons: " + ", ".join(data["lessons"]))
    if data.get("intentions_next_week"):
        parts.append("Intentions: " + ", ".join(data["intentions_next_week"]))
    return "\n".join(parts)


class WeeklyReviewSerializer(serializers.ModelSerializer):
    """User-facing serializer (JWT auth). Excludes tenant and auto-generates raw_text."""

    class Meta:
        model = WeeklyReview
        fields = (
            "id",
            "week_start",
            "week_end",
            "mood_summary",
            "top_wins",
            "top_challenges",
            "lessons",
            "week_rating",
            "intentions_next_week",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

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

    def create(self, validated_data: dict) -> WeeklyReview:
        tenant = self.context["tenant"]
        raw_text = _build_weekly_review_raw_text(validated_data)
        return WeeklyReview.objects.create(tenant=tenant, raw_text=raw_text, **validated_data)

    def update(self, instance: WeeklyReview, validated_data: dict) -> WeeklyReview:
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.raw_text = _build_weekly_review_raw_text(
            {f: getattr(instance, f) for f in (
                "mood_summary", "week_rating", "top_wins", "top_challenges", "lessons", "intentions_next_week",
            )}
        )
        instance.save()
        return instance


# ---------------------------------------------------------------------------
# Journal templates
# ---------------------------------------------------------------------------


class NoteTemplateSectionSerializer(serializers.Serializer):
    slug = serializers.CharField()
    title = serializers.CharField()
    content = serializers.CharField(allow_blank=True)
    source = serializers.ChoiceField(choices=NoteTemplate.Source.choices, required=False, default="shared")


class NoteTemplateSerializer(serializers.ModelSerializer):
    """User-facing note template serializer."""

    sections = NoteTemplateSectionSerializer(many=True)

    class Meta:
        model = NoteTemplate
        fields = (
            "id",
            "slug",
            "name",
            "sections",
            "is_default",
            "source",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_sections(self, value: list[dict]) -> list[dict[str, str]]:
        return _validate_template_sections(value)

    def create(self, validated_data: dict) -> NoteTemplate:
        tenant = self.context["tenant"]
        if validated_data.get("is_default"):
            NoteTemplate.objects.filter(tenant=tenant, is_default=True).update(is_default=False)
        return NoteTemplate.objects.create(tenant=tenant, **validated_data)

    def update(self, instance: NoteTemplate, validated_data: dict) -> NoteTemplate:
        sections = validated_data.get("sections")
        if sections is not None:
            instance.sections = sections
        if "is_default" in validated_data and validated_data["is_default"]:
            NoteTemplate.objects.filter(tenant=instance.tenant, is_default=True).exclude(pk=instance.pk).update(
                is_default=False,
            )

        for attr, value in validated_data.items():
            if attr == "sections":
                continue
            setattr(instance, attr, value)
        instance.save()
        return instance
