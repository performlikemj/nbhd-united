from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import AgentConfig, Tenant, User


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "plan_tier", "is_active", "created_at")
    list_filter = ("plan_tier", "is_active")
    search_fields = ("name", "slug")


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "display_name", "tenant", "telegram_chat_id", "is_active")
    list_filter = ("is_active", "tenant__plan_tier")
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Tenant", {"fields": ("tenant", "telegram_chat_id", "telegram_user_id", "display_name", "language", "preferences")}),
    )


@admin.register(AgentConfig)
class AgentConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "model_tier", "temperature")
    list_filter = ("model_tier",)
