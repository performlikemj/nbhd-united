"""Durable state for the EventKit gateway, generational mirror, and commands."""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import F, Q

from apps.tenants.models import Tenant


class AuthorizationStatus(models.TextChoices):
    NOT_DETERMINED = "not_determined", "Not determined"
    FULL_ACCESS = "full_access", "Full access"
    WRITE_ONLY = "write_only", "Write only"
    DENIED = "denied", "Denied"
    RESTRICTED = "restricted", "Restricted"
    UNAVAILABLE = "unavailable", "Unavailable"


class DatebookGateway(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        RETIRED = "retired", "Retired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="datebook_gateways")
    installation_id = models.CharField(max_length=64)
    gateway_epoch = models.PositiveBigIntegerField(default=1)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    current_generation = models.PositiveBigIntegerField(default=0)
    events_full_snapshot_required = models.BooleanField(default=True)
    reminders_full_snapshot_required = models.BooleanField(default=True)
    events_authorization = models.CharField(
        max_length=20,
        choices=AuthorizationStatus.choices,
        default=AuthorizationStatus.NOT_DETERMINED,
    )
    reminders_authorization = models.CharField(
        max_length=20,
        choices=AuthorizationStatus.choices,
        default=AuthorizationStatus.NOT_DETERMINED,
    )
    events_last_complete_sync_at = models.DateTimeField(null=True, blank=True)
    reminders_last_complete_sync_at = models.DateTimeField(null=True, blank=True)
    events_window_start = models.DateTimeField(null=True, blank=True)
    events_window_end = models.DateTimeField(null=True, blank=True)
    registered_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "datebook_gateways"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant"],
                condition=Q(status="active"),
                name="datebook_one_active_gateway",
            ),
            models.UniqueConstraint(
                fields=["tenant", "installation_id"],
                name="datebook_gateway_installation_unique",
            ),
            models.CheckConstraint(condition=Q(gateway_epoch__gte=1), name="datebook_gateway_epoch_positive"),
        ]
        indexes = [models.Index(fields=["tenant", "status"], name="datebook_gateway_active_idx")]


class DatebookDestinationDefault(models.Model):
    """One installation-scoped writable destination learned from owner approval."""

    class EntityType(models.TextChoices):
        CALENDAR = "calendar", "Calendar"
        REMINDER = "reminder", "Reminder"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="datebook_destination_defaults",
    )
    entity_type = models.CharField(max_length=16, choices=EntityType.choices)
    name = models.CharField(max_length=256)
    fingerprint = models.CharField(max_length=64)
    target_installation_id = models.CharField(max_length=64)
    gateway_epoch = models.PositiveBigIntegerField()
    pii_receipts = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "datebook_destination_defaults"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "entity_type"],
                name="datebook_dest_default_unique",
            ),
            models.CheckConstraint(
                condition=Q(gateway_epoch__gte=1),
                name="datebook_dest_default_epoch",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "entity_type"],
                name="datebook_dest_default_idx",
            )
        ]


class SyncRun(models.Model):
    class State(models.TextChoices):
        OPEN = "open", "Open"
        STAGED = "staged", "Staged"
        COMMITTED = "committed", "Committed"
        ABORTED = "aborted", "Aborted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="datebook_sync_runs")
    gateway = models.ForeignKey(DatebookGateway, on_delete=models.CASCADE, related_name="sync_runs")
    client_run_id = models.CharField(max_length=64)
    server_now = models.DateTimeField()
    event_window_start = models.DateTimeField()
    event_window_end = models.DateTimeField()
    base_generation = models.PositiveBigIntegerField()
    gateway_epoch = models.PositiveBigIntegerField()
    state = models.CharField(max_length=12, choices=State.choices, default=State.OPEN)
    events_in_scope = models.BooleanField(default=False)
    events_authorization = models.CharField(
        max_length=20,
        choices=AuthorizationStatus.choices,
        default=AuthorizationStatus.NOT_DETERMINED,
    )
    events_coverage_complete = models.BooleanField(default=False)
    events_committable = models.BooleanField(default=False)
    events_full_snapshot = models.BooleanField(default=False)
    reminders_in_scope = models.BooleanField(default=False)
    reminders_authorization = models.CharField(
        max_length=20,
        choices=AuthorizationStatus.choices,
        default=AuthorizationStatus.NOT_DETERMINED,
    )
    reminders_coverage_complete = models.BooleanField(default=False)
    reminders_committable = models.BooleanField(default=False)
    reminders_full_snapshot = models.BooleanField(default=False)
    events_manifest_digest = models.CharField(max_length=64, blank=True, default="")
    events_item_count = models.PositiveIntegerField(null=True, blank=True)
    reminders_manifest_digest = models.CharField(max_length=64, blank=True, default="")
    reminders_item_count = models.PositiveIntegerField(null=True, blank=True)
    events_absent_source_keys = models.JSONField(default=list, blank=True)
    reminders_absent_source_keys = models.JSONField(default=list, blank=True)
    commit_request_digest = models.CharField(max_length=64, blank=True, default="")
    published_generation = models.PositiveBigIntegerField(null=True, blank=True)
    requires_full_snapshot = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    staged_at = models.DateTimeField(null=True, blank=True)
    committed_at = models.DateTimeField(null=True, blank=True)
    aborted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "datebook_sync_runs"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "client_run_id"],
                name="datebook_sync_client_run_unique",
            ),
            models.CheckConstraint(
                condition=Q(event_window_end__gt=F("event_window_start")),
                name="datebook_sync_event_window_order",
            ),
            models.CheckConstraint(condition=Q(gateway_epoch__gte=1), name="datebook_sync_epoch_positive"),
            models.CheckConstraint(
                condition=(
                    Q(state__in=["open", "staged"], committed_at__isnull=True, aborted_at__isnull=True)
                    | Q(state="committed", committed_at__isnull=False, published_generation__isnull=False)
                    | Q(state="aborted", aborted_at__isnull=False, committed_at__isnull=True)
                ),
                name="datebook_sync_state_fields_legal",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "state"], name="datebook_sync_state_idx"),
            models.Index(fields=["gateway", "gateway_epoch"], name="datebook_sync_gateway_idx"),
        ]


class SyncPage(models.Model):
    """PII-free page receipt; staged bodies live invisibly on mirror rows."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="datebook_sync_pages")
    run = models.ForeignKey(SyncRun, on_delete=models.CASCADE, related_name="pages")
    page_index = models.PositiveIntegerField()
    request_digest = models.CharField(max_length=64)
    event_count = models.PositiveSmallIntegerField(default=0)
    reminder_count = models.PositiveSmallIntegerField(default=0)
    events_valid = models.BooleanField(default=True)
    reminders_valid = models.BooleanField(default=True)
    error_codes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "datebook_sync_pages"
        constraints = [
            models.UniqueConstraint(fields=["run", "page_index"], name="datebook_sync_page_unique"),
            models.CheckConstraint(
                condition=Q(event_count__lte=50, reminder_count__lte=50),
                name="datebook_sync_page_caps",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "run"], name="datebook_sync_page_run_idx")]


class SourceType(models.TextChoices):
    LOCAL = "local", "Local"
    ICLOUD = "icloud", "iCloud"
    EXCHANGE = "exchange", "Exchange"
    CALDAV = "caldav", "CalDAV"
    SUBSCRIBED = "subscribed", "Subscribed"
    BIRTHDAYS = "birthdays", "Birthdays"
    OTHER = "other", "Other"


class CalendarContext(models.Model):
    """A non-default per-calendar inclusion or owner-authored context row."""

    class EntityScope(models.TextChoices):
        EVENT = "event", "Event"
        REMINDER = "reminder", "Reminder"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="datebook_calendar_contexts",
    )
    entity_scope = models.CharField(max_length=16, choices=EntityScope.choices)
    calendar_fingerprint = models.CharField(max_length=64)
    included = models.BooleanField(default=True)
    container_title = models.CharField(max_length=256, blank=True, default="")
    source_title = models.CharField(max_length=256, blank=True, default="")
    source_type = models.CharField(max_length=16, choices=SourceType.choices, default=SourceType.OTHER)
    context_note = models.CharField(max_length=240, blank=True, default="")
    pii_receipts = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "datebook_calendar_contexts"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "entity_scope", "calendar_fingerprint"],
                name="datebook_calendar_context_unique",
            ),
            models.CheckConstraint(
                condition=Q(included=True) | Q(container_title="", source_title=""),
                name="datebook_context_excluded_titles_empty",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "entity_scope"],
                name="datebook_context_scope_idx",
            )
        ]


class TimeKind(models.TextChoices):
    ALL_DAY = "all_day", "All day"
    ZONED = "zoned", "Zoned"
    FLOATING = "floating", "Floating"


def _event_time_condition() -> Q:
    return (
        Q(
            time_kind="all_day",
            all_day_start_date__isnull=False,
            all_day_end_date_exclusive__isnull=False,
            zoned_start_at__isnull=True,
            zoned_end_at__isnull=True,
            tz_id="",
            floating_start_date__isnull=True,
            floating_start_time__isnull=True,
            floating_end_date__isnull=True,
            floating_end_time__isnull=True,
        )
        | Q(
            time_kind="zoned",
            all_day_start_date__isnull=True,
            all_day_end_date_exclusive__isnull=True,
            zoned_start_at__isnull=False,
            zoned_end_at__isnull=False,
            floating_start_date__isnull=True,
            floating_start_time__isnull=True,
            floating_end_date__isnull=True,
            floating_end_time__isnull=True,
        )
        & ~Q(tz_id="")
        | Q(
            time_kind="floating",
            all_day_start_date__isnull=True,
            all_day_end_date_exclusive__isnull=True,
            zoned_start_at__isnull=True,
            zoned_end_at__isnull=True,
            tz_id="",
            floating_start_date__isnull=False,
            floating_start_time__isnull=False,
            floating_end_date__isnull=False,
            floating_end_time__isnull=False,
        )
    )


class MirrorEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="datebook_events")
    source_key = models.CharField(max_length=64)
    external_id = models.CharField(max_length=255, blank=True, default="")
    series_id = models.CharField(max_length=255, blank=True, default="")
    source_fingerprint = models.CharField(max_length=64, blank=True, default="")
    calendar_fingerprint = models.CharField(max_length=64, blank=True, default="")
    source_type = models.CharField(max_length=16, choices=SourceType.choices, default=SourceType.OTHER)
    source_title = models.CharField(max_length=256, blank=True, default="")
    calendar_title = models.CharField(max_length=256, blank=True, default="")
    is_read_only = models.BooleanField(default=False)
    authorization_status = models.CharField(
        max_length=20,
        choices=AuthorizationStatus.choices,
        default=AuthorizationStatus.NOT_DETERMINED,
    )
    time_kind = models.CharField(max_length=12, choices=TimeKind.choices, blank=True, default="")
    all_day_start_date = models.DateField(null=True, blank=True)
    all_day_end_date_exclusive = models.DateField(null=True, blank=True)
    zoned_start_at = models.DateTimeField(null=True, blank=True)
    zoned_end_at = models.DateTimeField(null=True, blank=True)
    tz_id = models.CharField(max_length=63, blank=True, default="")
    floating_start_date = models.DateField(null=True, blank=True)
    floating_start_time = models.TimeField(null=True, blank=True)
    floating_end_date = models.DateField(null=True, blank=True)
    floating_end_time = models.TimeField(null=True, blank=True)
    title = models.CharField(max_length=256, blank=True, default="")
    location = models.CharField(max_length=512, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    is_recurring = models.BooleanField(default=False)
    content_hash = models.CharField(max_length=64, blank=True, default="")
    active = models.BooleanField(default=False)
    first_seen_generation = models.PositiveBigIntegerField(default=0)
    last_seen_generation = models.PositiveBigIntegerField(default=0)
    inactive_generation = models.PositiveBigIntegerField(null=True, blank=True)
    staged_run = models.ForeignKey(
        SyncRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staged_events",
    )
    staged_page_index = models.PositiveIntegerField(null=True, blank=True)
    staged_payload = models.JSONField(default=dict, blank=True)
    pii_receipts = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "datebook_mirror_events"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "source_key"], name="datebook_event_source_unique"),
            models.CheckConstraint(
                condition=Q(content_hash="") | _event_time_condition(),
                name="datebook_event_tagged_time",
            ),
            models.CheckConstraint(
                condition=Q(content_hash="", active=False) | ~Q(content_hash=""),
                name="datebook_event_active_materialized",
            ),
            models.CheckConstraint(
                condition=(
                    Q(content_hash="")
                    | Q(time_kind="all_day", all_day_end_date_exclusive__gt=F("all_day_start_date"))
                    | Q(time_kind="zoned", zoned_end_at__gte=F("zoned_start_at"))
                    | Q(time_kind="floating", floating_end_date__gt=F("floating_start_date"))
                    | Q(
                        time_kind="floating",
                        floating_end_date=F("floating_start_date"),
                        floating_end_time__gte=F("floating_start_time"),
                    )
                ),
                name="datebook_event_time_order",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "active"], name="datebook_event_active_idx"),
            models.Index(fields=["tenant", "staged_run"], name="datebook_event_stage_idx"),
            models.Index(fields=["tenant", "zoned_start_at"], name="datebook_event_zoned_idx"),
            models.Index(fields=["tenant", "all_day_start_date"], name="datebook_event_day_idx"),
        ]


class DueKind(models.TextChoices):
    NONE = "none", "None"
    ALL_DAY = "all_day", "All day"
    ZONED = "zoned", "Zoned"
    FLOATING = "floating", "Floating"


def _reminder_due_condition() -> Q:
    return (
        Q(
            due_kind="none",
            due_date__isnull=True,
            zoned_due_at__isnull=True,
            due_tz_id="",
            floating_due_date__isnull=True,
            floating_due_time__isnull=True,
        )
        | Q(
            due_kind="all_day",
            due_date__isnull=False,
            zoned_due_at__isnull=True,
            due_tz_id="",
            floating_due_date__isnull=True,
            floating_due_time__isnull=True,
        )
        | Q(
            due_kind="zoned",
            due_date__isnull=True,
            zoned_due_at__isnull=False,
            floating_due_date__isnull=True,
            floating_due_time__isnull=True,
        )
        & ~Q(due_tz_id="")
        | Q(
            due_kind="floating",
            due_date__isnull=True,
            zoned_due_at__isnull=True,
            due_tz_id="",
            floating_due_date__isnull=False,
            floating_due_time__isnull=False,
        )
    )


class MirrorReminder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="datebook_reminders")
    source_key = models.CharField(max_length=64)
    external_id = models.CharField(max_length=255, blank=True, default="")
    series_id = models.CharField(max_length=255, blank=True, default="")
    source_fingerprint = models.CharField(max_length=64, blank=True, default="")
    calendar_fingerprint = models.CharField(max_length=64, blank=True, default="")
    source_type = models.CharField(max_length=16, choices=SourceType.choices, default=SourceType.OTHER)
    source_title = models.CharField(max_length=256, blank=True, default="")
    list_title = models.CharField(max_length=256, blank=True, default="")
    is_read_only = models.BooleanField(default=False)
    authorization_status = models.CharField(
        max_length=20,
        choices=AuthorizationStatus.choices,
        default=AuthorizationStatus.NOT_DETERMINED,
    )
    due_kind = models.CharField(max_length=12, choices=DueKind.choices, blank=True, default="")
    due_date = models.DateField(null=True, blank=True)
    zoned_due_at = models.DateTimeField(null=True, blank=True)
    due_tz_id = models.CharField(max_length=63, blank=True, default="")
    floating_due_date = models.DateField(null=True, blank=True)
    floating_due_time = models.TimeField(null=True, blank=True)
    title = models.CharField(max_length=256, blank=True, default="")
    location = models.CharField(max_length=512, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    priority = models.PositiveSmallIntegerField(default=0)
    content_hash = models.CharField(max_length=64, blank=True, default="")
    active = models.BooleanField(default=False)
    first_seen_generation = models.PositiveBigIntegerField(default=0)
    last_seen_generation = models.PositiveBigIntegerField(default=0)
    inactive_generation = models.PositiveBigIntegerField(null=True, blank=True)
    staged_run = models.ForeignKey(
        SyncRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staged_reminders",
    )
    staged_page_index = models.PositiveIntegerField(null=True, blank=True)
    staged_payload = models.JSONField(default=dict, blank=True)
    pii_receipts = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "datebook_mirror_reminders"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "source_key"], name="datebook_reminder_source_unique"),
            models.CheckConstraint(
                condition=Q(content_hash="") | _reminder_due_condition(),
                name="datebook_reminder_tagged_due",
            ),
            models.CheckConstraint(
                condition=Q(content_hash="", active=False) | ~Q(content_hash=""),
                name="datebook_reminder_active_materialized",
            ),
            models.CheckConstraint(condition=Q(priority__lte=9), name="datebook_reminder_priority_range"),
            models.CheckConstraint(
                condition=(
                    Q(content_hash="")
                    | Q(completed=True, completed_at__isnull=False)
                    | Q(completed=False, completed_at__isnull=True)
                ),
                name="datebook_reminder_completion_time",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "active"], name="datebook_reminder_active_idx"),
            models.Index(fields=["tenant", "staged_run"], name="datebook_reminder_stage_idx"),
            models.Index(fields=["tenant", "completed"], name="datebook_reminder_done_idx"),
        ]


class DeviceCommand(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        LEASED = "leased", "Leased"
        EXECUTING = "executing", "Executing"
        EXECUTED = "executed", "Executed"
        FAILED = "failed", "Failed"
        AMBIGUOUS = "ambiguous", "Ambiguous"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    class CommandType(models.TextChoices):
        CALENDAR_CREATE = "calendar_create", "Calendar create"
        REMINDER_CREATE = "reminder_create", "Reminder create"

    class ExecutionStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        EXECUTING = "executing", "Executing"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        AMBIGUOUS = "ambiguous", "Ambiguous"

    class MirrorStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SYNCED = "synced", "Synced"
        FAILED = "failed", "Failed"
        NOT_REQUESTED = "not_requested", "Not requested"

    class SafeError(models.TextChoices):
        NONE = "", "None"
        NEEDS_AUTHORIZATION = "needs_authorization", "Needs authorization"
        LIST_NOT_FOUND = "list_not_found", "List not found"
        LIST_AMBIGUOUS = "list_ambiguous", "List ambiguous"
        LIST_READ_ONLY = "list_read_only", "List read only"
        CALENDAR_NOT_FOUND = "calendar_not_found", "Calendar not found"
        CALENDAR_AMBIGUOUS = "calendar_ambiguous", "Calendar ambiguous"
        CALENDAR_READ_ONLY = "calendar_read_only", "Calendar read only"
        DESTINATION_CHANGED = "destination_changed", "Destination changed"
        INVALID_PAYLOAD = "invalid_payload", "Invalid payload"
        SAVE_FAILED = "save_failed", "Save failed"
        UNKNOWN_SAFE_FAILURE = "unknown_safe_failure", "Unknown safe failure"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="datebook_commands")
    request_id = models.CharField(max_length=128)
    request_digest = models.CharField(max_length=64)
    command_type = models.CharField(max_length=24, choices=CommandType.choices)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    item_count = models.PositiveSmallIntegerField(default=1)
    target_installation_id = models.CharField(max_length=64)
    target_gateway_epoch = models.PositiveBigIntegerField()
    destination_fingerprint = models.CharField(max_length=64, blank=True, default="")
    destination_name = models.CharField(max_length=256, blank=True, default="")
    display_text = models.CharField(max_length=512, blank=True, default="")
    payload = models.JSONField(default=dict)
    pii_receipts = models.JSONField(default=dict, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    execution_deadline_at = models.DateTimeField(null=True, blank=True)
    execution_status = models.CharField(
        max_length=12,
        choices=ExecutionStatus.choices,
        default=ExecutionStatus.PENDING,
    )
    mirror_status = models.CharField(
        max_length=16,
        choices=MirrorStatus.choices,
        default=MirrorStatus.PENDING,
    )
    safe_error = models.CharField(max_length=32, choices=SafeError.choices, blank=True, default="")
    result_id = models.CharField(max_length=64, blank=True, default="")
    result_request_digest = models.CharField(max_length=64, blank=True, default="")
    journaled_at = models.DateTimeField(null=True, blank=True)
    result_identifiers = models.JSONField(default=dict, blank=True)
    result_display = models.CharField(max_length=512, blank=True, default="")
    expires_at = models.DateTimeField()
    target_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "datebook_device_commands"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "request_id"], name="datebook_command_request_unique"),
            models.CheckConstraint(
                condition=Q(item_count__gte=1, item_count__lte=5),
                name="datebook_command_item_cap",
            ),
            models.CheckConstraint(condition=Q(target_gateway_epoch__gte=1), name="datebook_command_epoch_positive"),
            models.CheckConstraint(
                condition=(
                    Q(
                        state="pending",
                        lease_token__isnull=True,
                        lease_expires_at__isnull=True,
                        started_at__isnull=True,
                        execution_deadline_at__isnull=True,
                        resolved_at__isnull=True,
                        execution_status="pending",
                        mirror_status="pending",
                        safe_error="",
                        result_id="",
                        journaled_at__isnull=True,
                    )
                    | Q(
                        state="leased",
                        lease_token__isnull=False,
                        lease_expires_at__isnull=False,
                        started_at__isnull=True,
                        execution_deadline_at__isnull=True,
                        resolved_at__isnull=True,
                        execution_status="pending",
                        mirror_status="pending",
                        safe_error="",
                        result_id="",
                        journaled_at__isnull=True,
                    )
                    | Q(
                        state="executing",
                        lease_token__isnull=False,
                        lease_expires_at__isnull=False,
                        started_at__isnull=False,
                        execution_deadline_at__isnull=False,
                        resolved_at__isnull=True,
                        execution_status="executing",
                        mirror_status="pending",
                        safe_error="",
                        result_id="",
                        journaled_at__isnull=True,
                    )
                    | Q(
                        state="executed",
                        started_at__isnull=False,
                        resolved_at__isnull=False,
                        execution_status="succeeded",
                        safe_error="",
                        journaled_at__isnull=False,
                    )
                    & ~Q(result_id="")
                    | Q(
                        state="failed",
                        started_at__isnull=False,
                        resolved_at__isnull=False,
                        execution_status="failed",
                        journaled_at__isnull=False,
                    )
                    & ~Q(safe_error="")
                    & ~Q(result_id="")
                    | Q(
                        state="ambiguous",
                        started_at__isnull=False,
                        resolved_at__isnull=False,
                        execution_status="ambiguous",
                        safe_error="",
                        result_id="",
                        journaled_at__isnull=True,
                    )
                    | Q(
                        state__in=["expired", "cancelled"],
                        lease_token__isnull=True,
                        lease_expires_at__isnull=True,
                        started_at__isnull=True,
                        execution_deadline_at__isnull=True,
                        resolved_at__isnull=False,
                        execution_status="pending",
                        mirror_status="pending",
                        safe_error="",
                        result_id="",
                        journaled_at__isnull=True,
                    )
                ),
                name="datebook_command_state_fields_legal",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "state", "created_at"], name="datebook_command_claim_idx"),
            models.Index(fields=["state", "lease_expires_at"], name="datebook_command_lease_idx"),
            models.Index(fields=["state", "execution_deadline_at"], name="datebook_command_exec_idx"),
        ]
