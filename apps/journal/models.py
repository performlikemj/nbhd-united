"""Journal and weekly review persistence models."""
from __future__ import annotations

import uuid

from django.db import models

from apps.journal import encryption
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
    is_encrypted = models.BooleanField(default=False)
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

    def _tenant_has_encryption_ref(self) -> bool:
        if not self.tenant_id:
            return False
        return bool(self.tenant.encryption_key_ref)

    def _normalize_plaintext(self, value: str) -> str:
        return "" if value is None else str(value)

    def _needs_encryption(self, value: str) -> bool:
        if value is None:
            return False
        text = str(value)
        if text == "":
            return False
        return not encryption._is_encryption_format(text)

    def decrypt(self) -> dict[str, str]:
        """Return plaintext title and markdown for this row."""
        if not self.is_encrypted:
            return {
                "title": self._normalize_plaintext(self.title),
                "markdown": self._normalize_plaintext(self.markdown),
            }

        if not self.tenant_id:
            raise ValueError("Cannot decrypt document: tenant is not set")

        key = encryption.get_tenant_key(self.tenant_id)
        plaintext_title = encryption.decrypt(self.title, key) if self.title else ""
        plaintext_markdown = encryption.decrypt(self.markdown, key) if self.markdown else ""
        return {
            "title": plaintext_title,
            "markdown": plaintext_markdown,
        }

    @property
    def title_plaintext(self) -> str:
        return self.decrypt().get("title", "")

    @property
    def markdown_plaintext(self) -> str:
        return self.decrypt().get("markdown", "")

    def _encrypt_if_needed(self) -> None:
        if not self._tenant_has_encryption_ref():
            return

        if not self.is_encrypted:
            key = encryption.get_tenant_key(self.tenant_id)
            self.title = encryption.encrypt(self._normalize_plaintext(self.title), key)
            self.markdown = encryption.encrypt(self._normalize_plaintext(self.markdown), key)
            self.is_encrypted = True
            return

        if self._needs_encryption(self.title):
            key = encryption.get_tenant_key(self.tenant_id)
            self.title = encryption.encrypt(self._normalize_plaintext(self.title), key)

        if self._needs_encryption(self.markdown):
            key = encryption.get_tenant_key(self.tenant_id)
            self.markdown = encryption.encrypt(self._normalize_plaintext(self.markdown), key)

    def save(self, *args, **kwargs):
        self._encrypt_if_needed()
        super().save(*args, **kwargs)
