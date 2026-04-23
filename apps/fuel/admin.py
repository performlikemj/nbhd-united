from django.contrib import admin

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


@admin.register(Workout)
class WorkoutAdmin(admin.ModelAdmin):
    list_display = ["activity", "tenant", "category", "date", "status", "duration_minutes", "rpe"]
    list_filter = ["category", "status"]
    search_fields = ["activity", "tenant__user__display_name"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(BodyWeightLog)
class BodyWeightLogAdmin(admin.ModelAdmin):
    list_display = ["tenant", "date", "weight_kg"]
    search_fields = ["tenant__user__display_name"]
    readonly_fields = ["id", "created_at"]


@admin.register(FuelProfile)
class FuelProfileAdmin(admin.ModelAdmin):
    list_display = ["tenant", "onboarding_status", "fitness_level", "days_per_week", "created_at"]
    list_filter = ["onboarding_status"]
    search_fields = ["tenant__user__display_name"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(WorkoutTemplate)
class WorkoutTemplateAdmin(admin.ModelAdmin):
    list_display = ["name", "tenant", "category", "activity", "created_at"]
    list_filter = ["category"]
    search_fields = ["name", "tenant__user__display_name"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(PersonalRecord)
class PersonalRecordAdmin(admin.ModelAdmin):
    list_display = ["exercise_name", "tenant", "value", "previous_value", "metric", "date"]
    list_filter = ["category", "metric"]
    search_fields = ["exercise_name", "tenant__user__display_name"]
    readonly_fields = ["id", "created_at"]


@admin.register(FuelGoal)
class FuelGoalAdmin(admin.ModelAdmin):
    list_display = ["exercise_name", "tenant", "target_value", "metric", "target_date", "achieved_at"]
    search_fields = ["exercise_name", "tenant__user__display_name"]
    readonly_fields = ["id", "created_at"]


@admin.register(RestingHeartRateLog)
class RestingHeartRateLogAdmin(admin.ModelAdmin):
    list_display = ["tenant", "date", "bpm"]
    search_fields = ["tenant__user__display_name"]
    readonly_fields = ["id", "created_at"]


@admin.register(SleepLog)
class SleepLogAdmin(admin.ModelAdmin):
    list_display = ["tenant", "date", "duration_hours", "quality"]
    search_fields = ["tenant__user__display_name"]
    readonly_fields = ["id", "created_at"]


@admin.register(WorkoutPlan)
class WorkoutPlanAdmin(admin.ModelAdmin):
    list_display = ["name", "tenant", "status", "start_date", "weeks", "days_per_week", "created_at"]
    list_filter = ["status"]
    search_fields = ["name", "tenant__user__display_name"]
    readonly_fields = ["id", "created_at", "updated_at"]
