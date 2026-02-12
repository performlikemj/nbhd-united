"""Automation models for proactive assistant workflows."""
from __future__ import annotations

import uuid

from django.db import models

from apps.tenants.models import Tenant


class Automation(models.Model):
    class Kind(models.TextChoices):
        DAILY_BRIEF = "daily_brief", "Daily Brief"
        WEEKLY_REVIEW = "weekly_review", "Weekly Review"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"

    class ScheduleType(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="automations")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    timezone = models.CharField(max_length=64, default="UTC")
    schedule_type = models.CharField(max_length=16, choices=ScheduleType.choices)
    schedule_time = models.TimeField()
    schedule_days = models.JSONField(default=list, blank=True)
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "automations"
        indexes = [
            models.Index(fields=["tenant", "status", "next_run_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.tenant_id}:{self.status}"


class AutomationRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    class TriggerSource(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCHEDULE = "schedule", "Schedule"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    automation = models.ForeignKey(Automation, on_delete=models.CASCADE, related_name="runs")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="automation_runs")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    trigger_source = models.CharField(
        max_length=16, choices=TriggerSource.choices, default=TriggerSource.SCHEDULE
    )
    scheduled_for = models.DateTimeField()
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=255, unique=True)
    input_payload = models.JSONField(default=dict, blank=True)
    result_payload = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "automation_runs"
        indexes = [
            models.Index(fields=["tenant", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.automation_id}:{self.status}:{self.trigger_source}"
