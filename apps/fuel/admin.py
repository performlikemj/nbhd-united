from django.contrib import admin

from .models import BodyWeightLog, Workout


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
