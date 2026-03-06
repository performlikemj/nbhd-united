"""Action gating models — confirmation flow for irreversible agent actions."""

from datetime import timedelta

from django.db import models
from django.utils import timezone


class ActionType(models.TextChoices):
    GMAIL_TRASH = "gmail_trash", "Gmail: Trash Message"
    GMAIL_DELETE = "gmail_delete", "Gmail: Delete Message"
    GMAIL_SEND = "gmail_send", "Gmail: Send Email"
    CALENDAR_DELETE = "calendar_delete", "Calendar: Delete Event"
    DRIVE_DELETE = "drive_delete", "Drive: Delete File"
    TASK_DELETE = "task_delete", "Tasks: Delete Task"


class ActionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DENIED = "denied", "Denied"
    EXPIRED = "expired", "Expired"


def default_expires_at():
    return timezone.now() + timedelta(minutes=5)


class PendingAction(models.Model):
    """Tracks a destructive action awaiting user confirmation."""

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="pending_actions",
    )
    action_type = models.CharField(max_length=32, choices=ActionType.choices)
    action_payload = models.JSONField(
        help_text="Structured data: {message_id, subject, ...} or {event_id, title, ...}",
    )
    display_summary = models.CharField(
        max_length=500,
        help_text="Human-readable description shown in confirmation prompt.",
    )
    status = models.CharField(
        max_length=16,
        choices=ActionStatus.choices,
        default=ActionStatus.PENDING,
    )

    # Platform message tracking (for editing after response)
    platform_message_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Telegram message_id or LINE message_id for post-response edit.",
    )
    platform_channel = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="telegram, line, etc.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=default_expires_at)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return f"{self.tenant} | {self.get_action_type_display()} | {self.status}"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at and self.status == ActionStatus.PENDING


class GatePreference(models.Model):
    """Per-action-type confirmation preference for a tenant."""

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="gate_preferences",
    )
    action_type = models.CharField(max_length=32, choices=ActionType.choices)
    require_confirmation = models.BooleanField(
        default=True,
        help_text="If False, this action type is auto-approved for this tenant.",
    )

    class Meta:
        unique_together = ["tenant", "action_type"]

    def __str__(self):
        status = "gated" if self.require_confirmation else "auto-approve"
        return f"{self.tenant} | {self.get_action_type_display()} | {status}"


class ActionAuditLog(models.Model):
    """Permanent record of every gated action — approved, denied, or expired."""

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="action_audit_logs",
    )
    action_type = models.CharField(max_length=32, choices=ActionType.choices)
    action_payload = models.JSONField()
    display_summary = models.CharField(max_length=500)
    result = models.CharField(
        max_length=16,
        choices=ActionStatus.choices,
        help_text="Final outcome: approved, denied, or expired.",
    )
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.tenant} | {self.get_action_type_display()} | {self.result}"
