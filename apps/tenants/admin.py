from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Tenant, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "display_name", "telegram_chat_id", "is_active")
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Telegram", {"fields": (
            "telegram_chat_id", "telegram_user_id",
            "telegram_username", "display_name", "language", "preferences",
        )}),
    )


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = (
        "user", "status", "model_tier", "container_id",
        "messages_today", "messages_this_month", "created_at",
    )
    list_filter = ("status", "model_tier")
    search_fields = ("user__username", "user__display_name", "container_id")
    readonly_fields = ("id", "created_at", "updated_at", "provisioned_at")
