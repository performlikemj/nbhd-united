"""Fuel serializers — workout and body-weight API representations."""

from rest_framework import serializers

from .models import BodyWeightLog, FuelProfile, Workout


class FuelProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelProfile
        fields = [
            "id",
            "onboarding_status",
            "fitness_level",
            "goals",
            "limitations",
            "equipment",
            "days_per_week",
            "additional_context",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class WorkoutSerializer(serializers.ModelSerializer):
    class Meta:
        model = Workout
        fields = [
            "id",
            "date",
            "status",
            "category",
            "activity",
            "duration_minutes",
            "rpe",
            "notes",
            "detail_json",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_detail_json(self, value):
        """Basic shape validation per category."""
        if not isinstance(value, dict):
            raise serializers.ValidationError("detail_json must be an object.")
        return value

    def validate_rpe(self, value):
        if value is not None and not (1 <= value <= 10):
            raise serializers.ValidationError("RPE must be between 1 and 10.")
        return value

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class WorkoutStubSerializer(serializers.ModelSerializer):
    """Lightweight serializer for calendar day cells."""

    class Meta:
        model = Workout
        fields = ["id", "date", "category", "activity", "status", "duration_minutes", "rpe"]
        read_only_fields = fields


class BodyWeightLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = BodyWeightLog
        fields = ["id", "date", "weight_kg", "created_at"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)
