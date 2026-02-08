from django.contrib import admin

from .models import MonthlyBudget, UsageRecord


@admin.register(UsageRecord)
class UsageRecordAdmin(admin.ModelAdmin):
    list_display = ("event_type", "tenant", "input_tokens", "output_tokens", "model_used", "cost_estimate", "created_at")
    list_filter = ("event_type",)
    date_hierarchy = "created_at"


@admin.register(MonthlyBudget)
class MonthlyBudgetAdmin(admin.ModelAdmin):
    list_display = ("month", "budget_dollars", "spent_dollars", "is_capped")
    list_filter = ("is_capped",)
