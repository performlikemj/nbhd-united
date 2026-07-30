from django.contrib import admin
from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
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

    def log_deletions(self, request, queryset):
        # Django logs before calling delete_model/delete_queryset. Suppress that
        # optimistic entry: the custom hooks below write one per user only after
        # its hard-delete commits, so a partial bulk failure has accurate logs
        # for exactly the users that were actually deleted.
        return []

    @staticmethod
    def _deletion_log_snapshot(user):
        return {
            "content_type_id": ContentType.objects.get_for_model(
                user,
                for_concrete_model=False,
            ).pk,
            "object_id": str(user.pk),
            "object_repr": str(user)[:200],
        }

    @staticmethod
    def _write_deletion_log(request, snapshot) -> None:
        LogEntry.objects.create(
            user_id=request.user.pk,
            action_flag=DELETION,
            change_message="",
            **snapshot,
        )

    def has_delete_permission(self, request, obj=None):
        # A post-delete LogEntry must retain its non-null actor FK. Prevent
        # self-deletion so a successful account delete cannot be followed by an
        # impossible audit insert and a misleading 500.
        if obj is not None and obj.pk == request.user.pk:
            return False
        return super().has_delete_permission(request, obj)

    @staticmethod
    def _reject_actor_deletion(request, users) -> None:
        if any(user.pk == request.user.pk for user in users):
            raise PermissionDenied("Administrators cannot delete their own account.")

    def delete_model(self, request, obj):
        from .views import _do_hard_delete

        self._reject_actor_deletion(request, [obj])
        snapshot = self._deletion_log_snapshot(obj)
        _do_hard_delete(obj)
        self._write_deletion_log(request, snapshot)

    def delete_queryset(self, request, queryset):
        from .views import _do_hard_delete

        users = list(queryset)
        # Preflight every target before deleting any. If the acting admin is
        # selected, the whole bulk operation fails without partial deletion.
        self._reject_actor_deletion(request, users)
        for user in users:
            snapshot = self._deletion_log_snapshot(user)
            _do_hard_delete(user)
            self._write_deletion_log(request, snapshot)


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
