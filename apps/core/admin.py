from django.contrib import admin

from .models import CoreProfile, MeditationSession


@admin.register(CoreProfile)
class CoreProfileAdmin(admin.ModelAdmin):
    list_display = ["tenant", "onboarding_status", "preferred_voice", "daily_cron_enabled", "created_at"]
    list_filter = ["onboarding_status", "ambient_bed_enabled", "daily_cron_enabled"]
    search_fields = ["tenant__user__display_name"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(MeditationSession)
class MeditationSessionAdmin(admin.ModelAdmin):
    list_display = ["title", "tenant", "date", "status", "voice", "duration_ms", "created_at"]
    list_filter = ["status"]
    search_fields = ["title", "theme", "tenant__user__display_name"]
    readonly_fields = ["id", "created_at", "updated_at"]
