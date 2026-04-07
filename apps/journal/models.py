"""Journal and weekly review persistence models."""
from __future__ import annotations

import uuid

from django.db import models
from pgvector.django import VectorField

from apps.tenants.models import Tenant


class NoteTemplate(models.Model):
    """Per-tenant template definition for sectionized daily notes.

    `sections` stores a JSON list of section descriptors, each with:
    - slug: stable machine key
    - title: display heading in markdown
    - content: template seed content for the section
    - source: optional ownership hint (agent/human/shared)
    """

    class Source(models.TextChoices):
        AGENT = "agent", "Agent"
        HUMAN = "human", "Human"
        SHARED = "shared", "Shared"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="note_templates")
    slug = models.SlugField(max_length=64)
    name = models.CharField(max_length=128)
    sections = models.JSONField(default=list)
    is_default = models.BooleanField(default=False)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.SHARED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "note_templates"
        unique_together = [
            ("tenant", "slug"),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.slug}"


class JournalEntry(models.Model):
    class Energy(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="journal_entries")
    date = models.DateField()
    mood = models.CharField(max_length=255)
    energy = models.CharField(max_length=16, choices=Energy.choices)
    wins = models.JSONField(default=list, blank=True)
    challenges = models.JSONField(default=list, blank=True)
    reflection = models.TextField(blank=True, default="")
    raw_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "journal_entries"
        indexes = [
            models.Index(fields=["tenant", "date"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.date}"


class WeeklyReview(models.Model):
    class WeekRating(models.TextChoices):
        THUMBS_UP = "thumbs-up", "Thumbs Up"
        THUMBS_DOWN = "thumbs-down", "Thumbs Down"
        MEH = "meh", "Meh"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="weekly_reviews")
    week_start = models.DateField()
    week_end = models.DateField()
    mood_summary = models.TextField()
    top_wins = models.JSONField(default=list, blank=True)
    top_challenges = models.JSONField(default=list, blank=True)
    lessons = models.JSONField(default=list, blank=True)
    week_rating = models.CharField(max_length=16, choices=WeekRating.choices)
    intentions_next_week = models.JSONField(default=list, blank=True)
    raw_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "weekly_reviews"
        indexes = [
            models.Index(fields=["tenant", "week_start", "week_end"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.week_start}:{self.week_end}"


class DailyNote(models.Model):
    """One markdown document per tenant per date. Both human and agent append to it."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="daily_notes")
    date = models.DateField()
    markdown = models.TextField(default="")
    template = models.ForeignKey(
        NoteTemplate,
        on_delete=models.SET_NULL,
        related_name="notes",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["tenant", "date"]
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.date}"


class UserMemory(models.Model):
    """One markdown document per tenant — like MEMORY.md. Agent curates this."""

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="user_memory")
    markdown = models.TextField(default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.tenant_id}:memory"


class Document(models.Model):
    """One markdown document. Can be a daily note, goal, project, review, etc.

    This is the v2 unified model replacing DailyNote, JournalEntry, WeeklyReview,
    UserMemory, and NoteTemplate.
    """

    class Kind(models.TextChoices):
        DAILY = "daily", "Daily Note"
        WEEKLY = "weekly", "Weekly Review"
        MONTHLY = "monthly", "Monthly Review"
        GOAL = "goal", "Goal"
        PROJECT = "project", "Project"
        TASKS = "tasks", "Tasks"
        IDEAS = "ideas", "Ideas"
        MEMORY = "memory", "Memory"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="documents")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    slug = models.CharField(max_length=128)
    title = models.CharField(max_length=256)
    markdown = models.TextField(default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["tenant", "kind", "slug"]
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "kind"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.kind}:{self.slug}"


class PendingExtraction(models.Model):
    """A goal, task, or lesson extracted from a daily note, awaiting user approval.

    Created by the nightly extraction job. Delivered to the user via Telegram
    inline buttons. Auto-expires after 7 days if not actioned.
    """

    class Kind(models.TextChoices):
        LESSON = "lesson", "Lesson"
        GOAL = "goal", "Goal"
        TASK = "task", "Task"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DISMISSED = "dismissed", "Dismissed"
        EXPIRED = "expired", "Expired"
        UNDONE = "undone", "Undone"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="pending_extractions")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    text = models.TextField()
    tags = models.JSONField(default=list)
    confidence = models.CharField(max_length=8, default="medium")  # high | medium
    source_date = models.DateField(null=True, blank=True)          # date of daily note extracted from
    expires_at = models.DateTimeField()
    telegram_message_id = models.CharField(max_length=64, blank=True)
    lesson_id = models.BigIntegerField(null=True, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "journal_pending_extractions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"], name="journal_pen_tenant__a1d532_idx"),
            models.Index(fields=["tenant", "kind", "status"], name="journal_pen_tenant__44d381_idx"),
            models.Index(fields=["expires_at"], name="journal_pen_expires_396cb6_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.kind}:{str(self.id)[:8]}"


class DocumentChunk(models.Model):
    """A chunked, embedded portion of a Document for vector search.

    Daily notes are split into ~500-token sections and embedded nightly
    so the poller can do contextual recall at session start.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="doc_chunks")
    document = models.ForeignKey("journal.Document", on_delete=models.CASCADE, related_name="chunks")
    chunk_index = models.IntegerField()
    text = models.TextField()
    embedding = VectorField(dimensions=1536)
    source_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "journal_document_chunks"
        unique_together = ["document", "chunk_index"]
        indexes = [
            models.Index(fields=["tenant", "source_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.document_id}:chunk{self.chunk_index}"


class Workspace(models.Model):
    """A focused conversation context for a tenant.

    Each workspace maps to a separate OpenClaw session via the `user` param
    in /v1/chat/completions. Messages are routed to the active workspace's
    session, giving each domain (work, personal, translation, etc.) its own
    independent conversation history while sharing the same workspace directory.

    Max 4 per tenant (enforced in application layer).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="workspaces")
    name = models.CharField(max_length=60)
    slug = models.SlugField(max_length=60)
    description = models.TextField(
        blank=True,
        default="",
        help_text="What topics this workspace covers. Used for routing classification.",
    )
    description_embedding = VectorField(
        dimensions=1536,
        null=True,
        blank=True,
        help_text="Embedding of description for message classification.",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="The 'General' catch-all workspace. Cannot be deleted.",
    )
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "journal_workspaces"
        unique_together = [("tenant", "slug")]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.slug}"
