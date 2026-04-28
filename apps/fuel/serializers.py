"""Fuel serializers — workout and body-weight API representations."""

from rest_framework import serializers

from .models import (
    BodyWeightLog,
    FuelGoal,
    FuelProfile,
    PersonalRecord,
    RestingHeartRateLog,
    SleepLog,
    Workout,
    WorkoutPlan,
    WorkoutTemplate,
)


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
            "preferred_days",
            "preferred_time",
            "additional_context",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class WorkoutPlanSerializer(serializers.ModelSerializer):
    workout_count = serializers.IntegerField(read_only=True, default=0)
    completed_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = WorkoutPlan
        fields = [
            "id",
            "name",
            "status",
            "start_date",
            "weeks",
            "days_per_week",
            "schedule_json",
            "notes",
            "workout_count",
            "completed_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "workout_count", "completed_count", "created_at", "updated_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class WorkoutSerializer(serializers.ModelSerializer):
    plan_id = serializers.UUIDField(source="plan.id", read_only=True, default=None)
    plan_name = serializers.CharField(source="plan.name", read_only=True, default=None)
    # Optional at the field level so callers can supply scheduled_at instead;
    # validate() backfills date from scheduled_at when needed.
    date = serializers.DateField(required=False)

    class Meta:
        model = Workout
        fields = [
            "id",
            "date",
            "scheduled_at",
            "window_start_at",
            "window_end_at",
            "status",
            "source",
            "original_workout",
            "skip_reason",
            "category",
            "activity",
            "duration_minutes",
            "rpe",
            "notes",
            "notes_thread",
            "detail_json",
            "plan_id",
            "plan_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "plan_id", "plan_name", "created_at", "updated_at"]

    def validate_detail_json(self, value):
        """Basic shape validation per category."""
        if not isinstance(value, dict):
            raise serializers.ValidationError("detail_json must be an object.")
        return value

    def validate_rpe(self, value):
        if value is not None and not (1 <= value <= 10):
            raise serializers.ValidationError("RPE must be between 1 and 10.")
        return value

    def validate(self, attrs):
        # When scheduled_at is provided without an explicit date, derive date
        # from it in the tenant's local timezone — this keeps day-bucketed
        # queries (calendar, weekly summary) consistent with the time-of-day.
        if attrs.get("scheduled_at") and not attrs.get("date"):
            attrs["date"] = attrs["scheduled_at"].date()
        if not attrs.get("date") and not self.instance:
            raise serializers.ValidationError({"date": "Either date or scheduled_at is required."})
        return attrs

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class WorkoutStubSerializer(serializers.ModelSerializer):
    """Lightweight serializer for calendar day cells."""

    plan_id = serializers.UUIDField(source="plan.id", read_only=True, default=None)

    class Meta:
        model = Workout
        fields = [
            "id",
            "date",
            "scheduled_at",
            "category",
            "activity",
            "status",
            "duration_minutes",
            "rpe",
            "plan_id",
        ]
        read_only_fields = fields


class BodyWeightLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = BodyWeightLog
        fields = ["id", "date", "weight_kg", "created_at"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class WorkoutTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkoutTemplate
        fields = ["id", "name", "category", "activity", "duration_minutes", "detail_json", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class PersonalRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = PersonalRecord
        fields = ["id", "exercise_name", "category", "value", "previous_value", "metric", "date", "created_at"]
        read_only_fields = fields


class FuelGoalSerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelGoal
        fields = ["id", "exercise_name", "metric", "target_value", "target_date", "achieved_at", "created_at"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class RestingHeartRateLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = RestingHeartRateLog
        fields = ["id", "date", "bpm", "created_at"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class SleepLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = SleepLog
        fields = ["id", "date", "duration_hours", "quality", "notes", "created_at"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)
