from django.contrib import admin
from django.db.models import F
from django.utils.translation import gettext_lazy as _

from .models import MonthlyBudget, UsageRecord


@admin.register(UsageRecord)
class UsageRecordAdmin(admin.ModelAdmin):
    list_display = (
        "event_type",
        "tenant",
        "input_tokens",
        "output_tokens",
        "model_used",
        "cost_estimate",
        "created_at",
    )
    list_filter = ("event_type",)
    date_hierarchy = "created_at"


class OverBudgetFilter(admin.SimpleListFilter):
    """Filter MonthlyBudget rows by whether spent_dollars >= budget_dollars."""

    title = _("capped")
    parameter_name = "capped"

    def lookups(self, request, model_admin):
        return [
            ("yes", _("Yes")),
            ("no", _("No")),
        ]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(spent_dollars__gte=F("budget_dollars"))
        if self.value() == "no":
            return queryset.exclude(spent_dollars__gte=F("budget_dollars"))
        return queryset


@admin.register(MonthlyBudget)
class MonthlyBudgetAdmin(admin.ModelAdmin):
    list_display = ("month", "budget_dollars", "spent_dollars", "capped")
    list_filter = (OverBudgetFilter,)

    @admin.display(boolean=True, description="capped")
    def capped(self, obj):
        return obj.is_over_budget
