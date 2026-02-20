"""Lesson constellation models."""

from django.contrib.postgres.fields import ArrayField
from django.db import models
from pgvector.django import VectorField

from apps.tenants.models import Tenant


class Lesson(models.Model):
    """A single learning/insight approved by the user."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="lessons")

    # Core content
    text = models.TextField(help_text="The lesson/insight in 1-3 sentences")
    context = models.TextField(blank=True, help_text="Where this came from — conversation, article, experience")

    # Vector embedding for clustering/similarity
    embedding = VectorField(dimensions=1536, null=True, help_text="OpenAI text-embedding-3-small")

    # Categorization
    tags = ArrayField(models.CharField(max_length=100), default=list, help_text="Auto-generated + user-editable")
    cluster_id = models.IntegerField(null=True, help_text="Assigned by clustering algorithm")
    cluster_label = models.CharField(max_length=200, blank=True, help_text="Human-readable cluster name")

    # Provenance
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
    source_ref = models.CharField(max_length=500, blank=True, help_text="Reference to source (daily note date, URL, etc.)")

    # Approval flow
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

    # Sharing (Phase 2+)
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
