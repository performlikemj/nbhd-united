from django.contrib import admin
from django.utils import timezone

from .models import ContentReport


@admin.register(ContentReport)
class ContentReportAdmin(admin.ModelAdmin):
    list_display = ("id", "target_kind", "status", "created_at", "resolved_at", "reporter_tenant_id")
    list_filter = ("status", "target_kind")
    readonly_fields = (
        "id",
        "reporter_tenant",
        "reporter_user",
        "target_kind",
        "shared_lesson",
        "friend_message",
        "reason",
        "created_at",
        "resolved_at",
    )
    ordering = ("-created_at",)
    actions = ("mark_hidden", "mark_dismissed")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Mark hidden", permissions=["change"])
    def mark_hidden(self, request, queryset):
        updated = queryset.update(status="hidden", resolved_at=timezone.now())
        self.message_user(request, f"Marked {updated} report(s) hidden.")

    @admin.action(description="Mark dismissed", permissions=["change"])
    def mark_dismissed(self, request, queryset):
        updated = queryset.update(status="dismissed", resolved_at=timezone.now())
        self.message_user(request, f"Marked {updated} report(s) dismissed.")
