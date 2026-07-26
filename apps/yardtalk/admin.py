from django.contrib import admin
from django.db.models import Count

from .models import License, LicenseActivation


class LicenseActivationInline(admin.TabularInline):
    model = LicenseActivation
    extra = 0
    readonly_fields = ("device_id", "activated_at")
    can_delete = False


@admin.register(License)
class LicenseAdmin(admin.ModelAdmin):
    list_display = (
        "key",
        "status",
        "revocation_reason",
        "purchaser_email",
        "activation_count",
        "created_at",
    )
    list_filter = ("status", "revocation_reason")
    search_fields = (
        "key",
        "purchaser_email",
        "stripe_session_id",
        "stripe_customer_id",
        "stripe_payment_intent_id",
    )
    readonly_fields = (
        "key",
        "stripe_session_id",
        "stripe_customer_id",
        "stripe_payment_intent_id",
        "revocation_reason",
        "key_email_sent_at",
        "created_at",
    )
    actions = ("revoke_licenses",)
    inlines = (LicenseActivationInline,)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_activation_count=Count("activations"))

    @admin.display(description="activations", ordering="_activation_count")
    def activation_count(self, obj):
        return obj._activation_count

    @admin.action(description="Revoke selected licenses")
    def revoke_licenses(self, request, queryset):
        updated = queryset.exclude(status=License.Status.REVOKED).update(
            status=License.Status.REVOKED,
            revocation_reason=License.RevocationReason.MANUAL,
        )
        self.message_user(request, f"Revoked {updated} license(s).")
