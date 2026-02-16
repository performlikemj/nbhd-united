from django.contrib import admin
from .models import PlatformIssueLog


@admin.register(PlatformIssueLog)
class PlatformIssueLogAdmin(admin.ModelAdmin):
    list_display = (
        "tenant", "category", "severity", "tool_name",
        "summary_short", "resolved", "created_at",
    )
    list_filter = ("category", "severity", "resolved", "created_at")
    search_fields = ("summary", "detail", "tool_name")
    readonly_fields = ("id", "created_at")
    list_editable = ("resolved",)
    date_hierarchy = "created_at"

    def summary_short(self, obj):
        return obj.summary[:80]
    summary_short.short_description = "Summary"
