"""Journal and weekly review persistence models."""
from __future__ import annotations

import uuid

from django.db import models

from apps.tenants.models import Tenant


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
