from django.contrib import admin

from .models import ActionAuditLog, GatePreference, PendingAction


@admin.register(PendingAction)
class PendingActionAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "tenant",
        "action_type",
        "display_summary",
        "status",
        "created_at",
        "expires_at",
    ]
    list_filter = ["status", "action_type", "platform_channel"]
    search_fields = ["display_summary", "tenant__owner__email"]
    readonly_fields = ["created_at", "responded_at"]


@admin.register(GatePreference)
class GatePreferenceAdmin(admin.ModelAdmin):
    list_display = ["tenant", "action_type", "require_confirmation"]
    list_filter = ["action_type", "require_confirmation"]
    search_fields = ["tenant__owner__email"]


@admin.register(ActionAuditLog)
class ActionAuditLogAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "tenant",
        "action_type",
        "display_summary",
        "result",
        "created_at",
    ]
    list_filter = ["result", "action_type"]
    search_fields = ["display_summary", "tenant__owner__email"]
    readonly_fields = ["created_at", "responded_at"]
