"""Router models — message buffering for idle-hibernated tenants."""

import uuid

from django.db import models


class BufferedMessage(models.Model):
    """Messages received while a tenant's container was hibernated.

    When a user sends a message to a hibernated container, the raw webhook
    payload is stored here. After the container wakes (~45s), a QStash task
    forwards all buffered messages in order, then marks them delivered.
    """

    class Channel(models.TextChoices):
        TELEGRAM = "telegram"
        LINE = "line"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="buffered_messages",
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    payload = models.JSONField(
        help_text="Raw webhook payload (Telegram update or LINE event)",
    )
    user_text = models.TextField(
        blank=True,
        default="",
        help_text="Extracted user message text for logging (truncated)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    delivered = models.BooleanField(default=False)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Status(models.TextChoices):
        PENDING = "pending"
        DELIVERED = "delivered"
        FAILED = "failed"

    delivery_attempts = models.PositiveSmallIntegerField(
        default=0,
        help_text="Number of times deliver_buffered_messages has tried and failed to deliver this message.",
    )
    delivery_status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        help_text="Terminal state: 'delivered' on success, 'failed' after attempts cap reached.",
    )

    class Meta:
        db_table = "buffered_messages"
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"BufferedMessage({self.channel}, {self.delivery_status}, tenant={self.tenant_id})"
