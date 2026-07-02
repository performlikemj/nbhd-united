"""Core serializers — mindfulness profile and meditation sessions."""

from rest_framework import serializers

from .models import CoreProfile, MeditationSession

# The thumb signal the feedback UI writes. Empty clears a prior signal. Kept in
# sync with the frontend feedback control (thumbs up/down) and the render-side
# consumers of ``user_feedback``.
ALLOWED_USER_FEEDBACK = {"", "liked", "disliked", "skipped"}
# A gentle cap so a runaway paste can't bloat the row; the note is guidance, not
# an essay. Mirrors the ``additional_context`` sizing.
MAX_FEEDBACK_NOTE_CHARS = 2000


class CoreProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = CoreProfile
        fields = [
            "id",
            "onboarding_status",
            "preferred_voice",
            "preferred_duration_minutes",
            "ambient_bed_enabled",
            "daily_cron_enabled",
            "preferred_time",
            "additional_context",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "onboarding_status", "created_at", "updated_at"]


class MeditationSessionSerializer(serializers.ModelSerializer):
    """Read-mostly: audio + status are set by the render pipeline, not the client.

    The only client-writable fields are the feedback signals (``user_feedback``,
    ``feedback_note``) — everything else is authored by the compose/render
    pipeline and is read-only. ``feedback_at`` is stamped server-side.
    """

    class Meta:
        model = MeditationSession
        fields = [
            "id",
            "date",
            "status",
            "title",
            "theme",
            "voice",
            "model",
            "guidance_text",
            "audio_url",
            "ogg_url",
            "duration_ms",
            "ambient_bed",
            "error",
            "user_feedback",
            "feedback_note",
            "feedback_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "date",
            "status",
            "title",
            "theme",
            "voice",
            "model",
            "guidance_text",
            "audio_url",
            "ogg_url",
            "duration_ms",
            "ambient_bed",
            "error",
            "feedback_at",
            "created_at",
            "updated_at",
        ]

    def validate_user_feedback(self, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in ALLOWED_USER_FEEDBACK:
            raise serializers.ValidationError(
                f"must be one of {sorted(v for v in ALLOWED_USER_FEEDBACK if v)}, or empty to clear"
            )
        return normalized

    def validate_feedback_note(self, value: str) -> str:
        return (value or "").strip()[:MAX_FEEDBACK_NOTE_CHARS]
