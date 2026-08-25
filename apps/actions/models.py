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
    CALENDAR_CREATE = "calendar_create", "Calendar: Create Event"
    REMINDER_CREATE = "reminder_create", "Reminders: Create Apple Reminder"
    CRON_CREATE = "cron_create", "Scheduled Tasks: Create Task"


class ActionOriginKind(models.TextChoices):
    CRON = "cron", "Cron"
    UNKNOWN = "unknown", "Unknown"


class ActionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DENIED = "denied", "Denied"
    EXPIRED = "expired", "Expired"


class ActionAuditOutcome(models.TextChoices):
    """Immutable gate decisions and downstream execution transitions."""

    APPROVED = "approved", "Approved"
    DENIED = "denied", "Denied"
    EXPIRED = "expired", "Expired"
    QUEUED = "queued", "Queued"
    EXECUTED = "executed", "Executed"
    FAILED = "failed", "Failed"
    COMMAND_EXPIRED = "command_expired", "Command expired"
    CANCELLED = "cancelled", "Cancelled"
    AMBIGUOUS = "ambiguous", "Ambiguous"


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
    pii_receipts = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16,
        choices=ActionStatus.choices,
        default=ActionStatus.PENDING,
    )
    datebook_request_id = models.CharField(max_length=128, blank=True, default="")
    cron_request_id = models.CharField(max_length=128, blank=True, default="")
    datebook_command_id = models.UUIDField(null=True, blank=True, unique=True)
    origin_kind = models.CharField(
        max_length=8,
        choices=ActionOriginKind.choices,
        default=ActionOriginKind.UNKNOWN,
    )
    origin_cron_name = models.CharField(max_length=255, blank=True, default="")
    origin_run_id = models.CharField(max_length=64, blank=True, default="")
    suggestion_fingerprint = models.CharField(max_length=64, blank=True, default="", db_index=True)
    resolution_code = models.CharField(max_length=32, blank=True, default="")
    originating_channel = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text="Immutable request provenance: app, telegram, line, or blank legacy origin.",
    )
    delivery_state = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text="Truthful confirmation delivery fact; only sent implies a real platform message id.",
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
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "datebook_request_id"],
                condition=~models.Q(datebook_request_id=""),
                name="actions_datebook_request_unique",
            ),
            models.UniqueConstraint(
                fields=["tenant", "cron_request_id"],
                condition=~models.Q(cron_request_id=""),
                name="actions_cron_request_unique",
            ),
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
    """Permanent immutable record of gate and downstream action transitions."""

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="action_audit_logs",
    )
    action_type = models.CharField(max_length=32, choices=ActionType.choices)
    action_payload = models.JSONField()
    display_summary = models.CharField(max_length=500)
    pii_receipts = models.JSONField(default=dict, blank=True)
    result = models.CharField(
        max_length=16,
        choices=ActionAuditOutcome.choices,
        help_text="Immutable gate decision or downstream execution transition.",
    )
    datebook_command_id = models.UUIDField(null=True, blank=True)
    detail_code = models.CharField(max_length=32, blank=True, default="")
    requested_destination_fingerprint = models.CharField(max_length=64, blank=True, default="")
    approved_destination_fingerprint = models.CharField(max_length=64, blank=True, default="")
    default_destination_old_fingerprint = models.CharField(max_length=64, blank=True, default="")
    default_destination_new_fingerprint = models.CharField(max_length=64, blank=True, default="")
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.tenant} | {self.get_action_type_display()} | {self.result}"


class CronDispatch(models.Model):
    """Durable handoff from a committed cron approval to external dispatch."""

    action = models.OneToOneField(
        PendingAction,
        on_delete=models.CASCADE,
        related_name="cron_dispatch",
    )
    cron = models.OneToOneField(
        "cron.CronJob",
        on_delete=models.CASCADE,
        related_name="approval_dispatch",
    )
    kind = models.CharField(max_length=8)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.action_id}:{self.cron_id}:{self.kind}"
