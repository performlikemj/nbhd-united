"""Core serializers — mindfulness profile and meditation sessions."""

from rest_framework import serializers

from .models import CoreProfile, MeditationSession


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
        read_only_fields = ["id", "created_at", "updated_at"]


class MeditationSessionSerializer(serializers.ModelSerializer):
    """Read-mostly: audio + status are set by the render pipeline, not the client."""

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
            "user_feedback",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
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
            "created_at",
            "updated_at",
        ]
