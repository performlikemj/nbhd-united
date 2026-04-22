"""Work session models — structured activity data pushed by external apps."""

import uuid

from django.db import models

from apps.tenants.models import Tenant


class Session(models.Model):
    """A work session pushed by an external app (e.g. YardTalk).

    Each session represents a bounded work period on a project, with
    a summary, accomplishments, blockers, and next steps. The assistant
    reads these to maintain awareness of the user's work across projects.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="sessions")

    # Source identification
    source = models.CharField(
        max_length=128,
        help_text="App identifier + version, e.g. 'yardtalk-mac/1.0.0'",
    )

    # Project context
    project = models.CharField(
        max_length=256,
        db_index=True,
        help_text="Project name, e.g. 'acme-labs-presentation'",
    )
    project_type = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Optional project category, e.g. 'presentation_prep'",
    )

    # Session timing
    session_start = models.DateTimeField(help_text="When the work session began")
    session_end = models.DateTimeField(help_text="When the work session ended")

    # Content
    summary = models.TextField(help_text="AI-generated summary of the session")
    accomplishments = models.JSONField(
        default=list,
        blank=True,
        help_text="List of things accomplished during the session",
    )
    blockers = models.JSONField(
        default=list,
        blank=True,
        help_text="List of blockers encountered",
    )
    next_steps = models.JSONField(
        default=list,
        blank=True,
        help_text="List of planned next steps",
    )
    references = models.JSONField(
        default=dict,
        blank=True,
        help_text="External references: report_url, clip_ids, etc.",
    )

    # Metadata
    test_mode = models.BooleanField(
        default=False,
        help_text="True for dev/test pushes — can be bulk-purged",
    )
    schema_version = models.IntegerField(
        default=1,
        help_text="Payload schema version for forward compatibility",
    )
    idempotency_key = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Client-supplied Idempotency-Key for dedup",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "journal_sessions"
        ordering = ["-session_start"]
        indexes = [
            models.Index(fields=["tenant", "project", "-session_start"]),
            models.Index(fields=["tenant", "-session_start"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                condition=models.Q(idempotency_key__gt=""),
                name="unique_tenant_idempotency_key",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.project}:{self.session_start.date()}"
