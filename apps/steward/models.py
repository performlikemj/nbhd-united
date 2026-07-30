from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class EvidenceSource(models.TextChoices):
    GATEWAY_HEARTBEAT = "gateway_heartbeat", "Gateway heartbeat"
    CI_RUN = "ci_run", "CI run"
    ASC_VERSION_STATE = "asc_version_state", "App Store Connect version state"
    MJ_ACK = "mj_ack", "MJ acknowledgement"


class Expectation(models.Model):
    class Kind(models.TextChoices):
        HEARTBEAT = "heartbeat", "Heartbeat"
        DEADLINE = "deadline", "Deadline"
        RECURRENCE = "recurrence", "Recurrence"

    class State(models.TextChoices):
        ARMED = "armed", "Armed"
        SATISFIED = "satisfied", "Satisfied"
        MISSED = "missed", "Missed"
        RETIRED = "retired", "Retired"

    class OnMiss(models.TextChoices):
        URGENT = "urgent", "Urgent"
        DIGEST = "digest", "Digest"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    interval_s = models.PositiveIntegerField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    cron_expr = models.CharField(max_length=128, null=True, blank=True)
    grace_s = models.PositiveIntegerField()
    evidence_source = models.CharField(max_length=32, choices=EvidenceSource.choices)
    subject = models.CharField(max_length=128)
    state = models.CharField(max_length=16, choices=State.choices, default=State.ARMED)
    last_satisfied_at = models.DateTimeField(null=True, blank=True)
    miss_count = models.PositiveIntegerField(default=0)
    last_alerted_at = models.DateTimeField(null=True, blank=True)
    on_miss = models.CharField(max_length=16, choices=OnMiss.choices)
    owner = models.CharField(max_length=32, default="mj")

    class Meta:
        db_table = "steward_expectations"
        indexes = [
            models.Index(fields=["state", "kind"]),
            models.Index(fields=["evidence_source", "subject"]),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.kind == self.Kind.DEADLINE:
            if self.due_at is None:
                errors["due_at"] = "Deadline expectations require due_at."
            if self.interval_s is not None:
                errors["interval_s"] = "Deadline expectations cannot use interval_s."
            if self.cron_expr:
                errors["cron_expr"] = "cron_expr is not implemented in Phase 1."
        elif self.kind in (self.Kind.HEARTBEAT, self.Kind.RECURRENCE):
            if not self.interval_s:
                errors["interval_s"] = "Heartbeat and recurrence expectations require a positive interval_s."
            if self.due_at is not None:
                errors["due_at"] = "Interval expectations cannot use due_at."
            if self.cron_expr:
                errors["cron_expr"] = "cron_expr is not implemented in Phase 1."
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return f"{self.kind}:{self.subject} ({self.state})"


class EvidenceEvent(models.Model):
    class Trust(models.TextChoices):
        AUTHENTICATED_API = "authenticated_api", "Authenticated API"
        HOST_LOG = "host_log", "Host log"
        UNTRUSTED_TEXT = "untrusted_text", "Untrusted text"

    class Provenance(models.TextChoices):
        MJ = "mj", "MJ"
        COLLECTOR = "collector", "Collector"
        AGENT_PROPOSED = "agent_proposed", "Agent proposed"

    source = models.CharField(max_length=32, choices=EvidenceSource.choices)
    subject = models.CharField(max_length=128)
    occurred_at = models.DateTimeField()
    received_at = models.DateTimeField(default=timezone.now)
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured evidence metadata only; maximum 4KB and no user PII.",
    )
    fingerprint = models.CharField(max_length=128, unique=True)
    trust = models.CharField(max_length=24, choices=Trust.choices)
    provenance = models.CharField(max_length=24, choices=Provenance.choices)

    class Meta:
        db_table = "steward_evidence_events"
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["source", "subject", "-occurred_at"]),
        ]

    def save(self, *args, **kwargs) -> None:
        if not self._state.adding:
            raise ValidationError("EvidenceEvent is append-only and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("EvidenceEvent is append-only and cannot be deleted.")

    def __str__(self) -> str:
        return f"{self.source}:{self.subject}@{self.occurred_at.isoformat()}"
