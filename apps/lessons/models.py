"""Lesson constellation models — stars, connections, tutoring, and star journaling."""

import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models
from pgvector.django import VectorField

from apps.tenants.models import Tenant


class Lesson(models.Model):
    """A single learning/insight — a star in the user's personal galaxy."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="lessons")

    # ── Content ──────────────────────────────────────────────
    text = models.TextField(help_text="The lesson/insight in 1-3 sentences")
    context = models.TextField(
        blank=True,
        help_text="Where this came from — conversation, article, experience",
    )

    # ── Embedding & clustering ───────────────────────────────
    embedding = VectorField(dimensions=1536, null=True, help_text="OpenAI text-embedding-3-small")
    tags = ArrayField(models.CharField(max_length=100), default=list, help_text="Auto-generated + user-editable")
    cluster_id = models.IntegerField(null=True, help_text="Assigned by clustering algorithm")
    cluster_label = models.CharField(max_length=200, blank=True, help_text="Human-readable cluster name")

    # ── Provenance ───────────────────────────────────────────
    source_type = models.CharField(
        max_length=30,
        choices=[
            ("conversation", "Conversation"),
            ("journal", "Journal Entry"),
            ("reflection", "Reflection"),
            ("article", "Article/Reading"),
            ("experience", "Life Experience"),
        ],
    )
    source_ref = models.CharField(
        max_length=500,
        blank=True,
        help_text="Reference to source (daily note date, URL, etc.)",
    )

    # ── Approval flow ────────────────────────────────────────
    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending Approval"),
            ("approved", "Approved"),
            ("dismissed", "Dismissed"),
        ],
        default="pending",
    )
    suggested_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    # ── Galaxy position (2D, UMAP-computed) ──────────────────
    position_x = models.FloatField(null=True, blank=True)
    position_y = models.FloatField(null=True, blank=True)

    # ── Star lifecycle ───────────────────────────────────────
    star_stage = models.CharField(
        max_length=20,
        choices=[
            ("proto", "Proto-star"),
            ("ignited", "Ignited"),
            ("radiant", "Radiant"),
            ("supernova", "Supernova"),
        ],
        default="proto",
        help_text="Visual/cognitive stage in the constellation game",
    )
    tutoring_sessions_count = models.IntegerField(default=0)
    last_tutored_at = models.DateTimeField(null=True, blank=True)
    last_visited_at = models.DateTimeField(null=True, blank=True)
    galaxy_note = models.TextField(
        blank=True,
        help_text="Player's pinned note visible from the galaxy view",
    )

    # ── Sharing (Phase 2+) ───────────────────────────────────
    shared = models.BooleanField(default=False)
    shared_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "lessons"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "cluster_id"]),
            models.Index(fields=["tenant", "star_stage"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.id}"


class LessonConnection(models.Model):
    """An edge between two related lessons (auto-detected or user-created)."""

    from_lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name="connections_out")
    to_lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name="connections_in")

    similarity = models.FloatField(help_text="Cosine similarity score (0-1)")
    connection_type = models.CharField(
        max_length=30,
        choices=[
            ("similar", "Similar Topic"),
            ("builds_on", "Builds On"),
            ("contradicts", "Contradicts"),
            ("user_linked", "User Linked"),
        ],
        default="similar",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "lesson_connections"
        unique_together = [("from_lesson", "to_lesson")]

    def __str__(self) -> str:
        return f"{self.from_lesson_id}->{self.to_lesson_id} ({self.connection_type})"


class TutoringSession(models.Model):
    """A single tutoring interaction with a star.

    Records the full dialogue and player signals for the assistant
    to learn about the player's strengths, blind spots, and thinking style.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    star = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name="tutoring_sessions")

    # Full transcript as [{role, content, phase, timestamp}, ...]
    messages = models.JSONField(default=list)

    # Outcome
    phases_completed = ArrayField(
        models.CharField(max_length=50),
        default=list,
        help_text="Phases completed in order: restate, deepen, stress_test, connect, apply",
    )
    mastery_achieved = models.BooleanField(default=False)
    new_star_stage = models.CharField(max_length=20, null=True, blank=True)
    skipped = models.BooleanField(default=False)

    # Player signals for assistant learning
    player_restated_accurately = models.BooleanField(null=True)
    player_found_edge_cases = models.BooleanField(null=True)
    connections_made = models.JSONField(default=list)  # [{to_star_id, player_text}]
    topic_shifted = models.CharField(max_length=100, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tutoring_sessions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["star", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.star_id}:{str(self.id)[:8]}"


class StarJournalEntry(models.Model):
    """A journal entry attached to a star — written after tutoring or re-visiting.

    Distinct from the main journal app — these are star-scoped reflections
    that orbit the star like planets. They feed into richer tutoring sessions
    and the mindfulness feature.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="star_journal_entries")
    star = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name="journal_entries",
    )

    text = models.TextField()
    entry_type = models.CharField(
        max_length=20,
        choices=[
            ("tutoring", "Written after a tutoring session"),
            ("revisit", "Written when revisiting the star"),
            ("free", "Free reflection on this topic"),
        ],
        default="free",
    )
    tags = ArrayField(models.CharField(max_length=100), default=list)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "star_journal_entries"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["star", "created_at"]),
            models.Index(fields=["tenant", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.star_id}:{str(self.id)[:8]}"
