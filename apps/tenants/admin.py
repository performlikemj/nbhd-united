from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect

from .models import Tenant, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "display_name", "telegram_chat_id", "is_active")
    fieldsets = BaseUserAdmin.fieldsets + (
        (
            "Telegram",
            {
                "fields": (
                    "telegram_chat_id",
                    "telegram_user_id",
                    "telegram_username",
                    "display_name",
                    "language",
                    "preferences",
                )
            },
        ),
    )

    @method_decorator(csrf_protect)
    def delete_view(self, request, object_id, extra_context=None):
        """Keep hard-delete's Azure teardown outside Django admin's atomic."""

        if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            return super().delete_view(request, object_id, extra_context)
        # ModelAdmin.delete_view normally wraps confirmed POSTs in atomic().
        # _do_hard_delete must deprovision Azure before the Tenant row
        # disappears, so execute the same protected implementation without
        # that wrapper and preserve the platform no-external-HTTP-in-atomic
        # invariant.
        return self._delete_view(request, object_id, extra_context)

    def delete_model(self, request, obj):
        from .views import _do_hard_delete

        _do_hard_delete(obj)

    def delete_queryset(self, request, queryset):
        from .views import _do_hard_delete

        for user in list(queryset):
            _do_hard_delete(user)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "status",
        "model_tier",
        "is_budget_exempt",
        "container_id",
        "messages_today",
        "messages_this_month",
        "created_at",
    )
    list_filter = ("status", "model_tier", "is_budget_exempt")
    search_fields = ("user__username", "user__display_name", "container_id")
    readonly_fields = ("id", "created_at", "updated_at", "provisioned_at")
