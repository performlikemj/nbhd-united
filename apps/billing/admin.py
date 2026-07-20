from django.contrib import admin
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import MonthlyBudget, UsageRecord, YardTalkLicense


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


@admin.register(YardTalkLicense)
class YardTalkLicenseAdmin(admin.ModelAdmin):
    list_display = ("key", "email", "created_at", "revoked_at", "device_count")
    search_fields = ("key", "email")
    actions = ("revoke_licenses", "resend_key_email")

    @admin.display(description="devices")
    def device_count(self, obj):
        return len(obj.device_ids or [])

    @admin.action(description="Revoke selected licenses")
    def revoke_licenses(self, request, queryset):
        updated = queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
        self.message_user(request, f"Revoked {updated} license(s).")

    @admin.action(description="Resend key email")
    def resend_key_email(self, request, queryset):
        from .yardtalk_licensing import send_license_key_email

        sent = sum(1 for lic in queryset if send_license_key_email(lic))
        self.message_user(request, f"Re-sent {sent} key email(s).")
