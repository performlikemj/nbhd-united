"""Serializers for journal artifacts."""

from __future__ import annotations

from rest_framework import serializers

from apps.pii.store_authoring import OwnerStoreSerializerMixin, author_store_fields

from .models import JournalEntry, NoteTemplate, WeeklyReview
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


def _normalize_week_rating(value: str) -> str:
    normalized = "-".join(value.strip().lower().split())
    if normalized not in WeeklyReview.WeekRating.values:
        choices = ", ".join(WeeklyReview.WeekRating.values)
        raise serializers.ValidationError(f"week_rating must be one of: {choices}.")
    return normalized


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
        from .store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            validated_data,
            model_label="journal.JournalEntry",
            seam="journal.entry.create.runtime",
            writer="runtime",
            defer_detection=True,
        )
        return JournalEntry.objects.create(tenant=tenant, pii_receipts=receipts, **authored)


class WeeklyReviewRuntimeSerializer(serializers.ModelSerializer):
    # Override ModelSerializer's ChoiceField so common model-written variants
    # can normalize before choice validation rejects them.
    week_rating = serializers.CharField()

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

    def validate_week_rating(self, value: str) -> str:
        return _normalize_week_rating(value)

    def validate_raw_text(self, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise serializers.ValidationError("raw_text is required.")
        return normalized

    def create(self, validated_data: dict) -> WeeklyReview:
        tenant = self.context["tenant"]
        from .store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            validated_data,
            model_label="journal.WeeklyReview",
            seam="journal.weekly_review.create.runtime",
            writer="runtime",
            defer_detection=True,
        )
        return WeeklyReview.objects.create(tenant=tenant, pii_receipts=receipts, **authored)


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
            "pii_receipts",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "pii_receipts", "created_at", "updated_at")

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
        from .store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            {**validated_data, "raw_text": raw_text},
            model_label="journal.JournalEntry",
            seam="journal.entry.create.owner",
            writer="owner",
        )
        return JournalEntry.objects.create(tenant=tenant, pii_receipts=receipts, **authored)

    def update(self, instance: JournalEntry, validated_data: dict) -> JournalEntry:
        tenant = self.context["tenant"]
        final_data = {
            field: validated_data.get(field, getattr(instance, field))
            for field in ("mood", "energy", "wins", "challenges", "reflection")
        }
        from .store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            {**validated_data, "raw_text": _build_raw_text(final_data)},
            model_label="journal.JournalEntry",
            seam="journal.entry.update.owner",
            writer="owner",
            receipts=instance.pii_receipts,
        )
        for attr, value in authored.items():
            setattr(instance, attr, value)
        instance.pii_receipts = receipts
        instance.save(update_fields=[*authored, "pii_receipts", "updated_at"])
        return instance

    def to_representation(self, instance):
        data = super().to_representation(instance)
        tenant = self.context.get("tenant")
        if tenant is None:
            return data
        from .store_authoring import owner_store_representation

        return owner_store_representation(instance, tenant, data, model_label="journal.JournalEntry")


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

    week_rating = serializers.CharField()

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
            "pii_receipts",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "pii_receipts", "created_at", "updated_at")

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

    def validate_week_rating(self, value: str) -> str:
        return _normalize_week_rating(value)

    def create(self, validated_data: dict) -> WeeklyReview:
        tenant = self.context["tenant"]
        raw_text = _build_weekly_review_raw_text(validated_data)
        from .store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            {**validated_data, "raw_text": raw_text},
            model_label="journal.WeeklyReview",
            seam="journal.weekly_review.create.owner",
            writer="owner",
        )
        return WeeklyReview.objects.create(tenant=tenant, pii_receipts=receipts, **authored)

    def update(self, instance: WeeklyReview, validated_data: dict) -> WeeklyReview:
        tenant = self.context["tenant"]
        fields = (
            "mood_summary",
            "week_rating",
            "top_wins",
            "top_challenges",
            "lessons",
            "intentions_next_week",
        )
        final_data = {field: validated_data.get(field, getattr(instance, field)) for field in fields}
        from .store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            {**validated_data, "raw_text": _build_weekly_review_raw_text(final_data)},
            model_label="journal.WeeklyReview",
            seam="journal.weekly_review.update.owner",
            writer="owner",
            receipts=instance.pii_receipts,
        )
        for attr, value in authored.items():
            setattr(instance, attr, value)
        instance.pii_receipts = receipts
        instance.save(update_fields=[*authored, "pii_receipts", "updated_at"])
        return instance

    def to_representation(self, instance):
        data = super().to_representation(instance)
        tenant = self.context.get("tenant")
        if tenant is None:
            return data
        from .store_authoring import owner_store_representation

        return owner_store_representation(instance, tenant, data, model_label="journal.WeeklyReview")


# ---------------------------------------------------------------------------
# Journal templates
# ---------------------------------------------------------------------------


class NoteTemplateSectionSerializer(serializers.Serializer):
    slug = serializers.CharField()
    title = serializers.CharField()
    content = serializers.CharField(allow_blank=True)
    source = serializers.ChoiceField(choices=NoteTemplate.Source.choices, required=False, default="shared")


class NoteTemplateSerializer(OwnerStoreSerializerMixin, serializers.ModelSerializer):
    """User-facing note template serializer."""

    pii_model_label = "journal.NoteTemplate"

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
            "pii_receipts",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "pii_receipts", "created_at", "updated_at")

    def validate_sections(self, value: list[dict]) -> list[dict[str, str]]:
        return _validate_template_sections(value)

    def create(self, validated_data: dict) -> NoteTemplate:
        tenant = self.context["tenant"]
        validated_data, receipts = author_store_fields(
            tenant,
            validated_data,
            model_label="journal.NoteTemplate",
            seam="journal.note_template.owner_create",
            writer="owner",
        )
        if validated_data.get("is_default"):
            NoteTemplate.objects.filter(tenant=tenant, is_default=True).update(is_default=False)
        return NoteTemplate.objects.create(tenant=tenant, pii_receipts=receipts, **validated_data)

    def update(self, instance: NoteTemplate, validated_data: dict) -> NoteTemplate:
        validated_data, receipts = author_store_fields(
            instance.tenant,
            validated_data,
            model_label="journal.NoteTemplate",
            seam="journal.note_template.owner_update",
            writer="owner",
            receipts=instance.pii_receipts,
        )
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
        instance.pii_receipts = receipts
        instance.save()
        return instance
