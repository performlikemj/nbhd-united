from django.contrib import admin

from .models import Plan, UsageEvent


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "tier", "monthly_price", "token_budget", "is_active")
    list_filter = ("tier", "is_active")


@admin.register(UsageEvent)
class UsageEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "tenant", "tokens", "model_used", "cost_estimate", "created_at")
    list_filter = ("event_type",)
    date_hierarchy = "created_at"
