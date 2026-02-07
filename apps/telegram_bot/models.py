import uuid

from django.db import models

from apps.tenants.models import Tenant


class TelegramBinding(models.Model):
    """Binds a Telegram chat to a tenant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="telegram_binding")
    chat_id = models.BigIntegerField(unique=True, db_index=True)
    username = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    bound_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "telegram_bindings"

    def __str__(self):
        return f"@{self.username or self.chat_id} → {self.tenant}"
