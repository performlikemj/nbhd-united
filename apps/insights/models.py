"""Models for the assistant baseline / insights subsystem.

Phase 0 covers: pillar snapshots (append-only time series), topic registry
plus aliases (curated taxonomy with synonym collapsing), assistant insights
(the memory layer the assistant accumulates about a tenant), and per-user
voice preferences (tone / volume / register-offset overrides).

Goals/intents are an extension of the existing Document model in
``apps.journal`` (see the migration there) — same data, two access paths.
"""

from __future__ import annotations

import uuid

from django.db import models

from apps.tenants.models import Tenant

from .pillars import Pillar


class PillarSnapshot(models.Model):
    """Append-only time series of pillar state per tenant.

    Payload mirrors the shape the corresponding pillar tab renders. Used for
    historical querying ("what did dining look like in February?"); live state
    is answered by direct data queries elsewhere.
    """

    class Granularity(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="pillar_snapshots")
    pillar = models.CharField(max_length=32, choices=Pillar.choices)
    ts = models.DateTimeField()
    granularity = models.CharField(max_length=16, choices=Granularity.choices)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "insights_pillar_snapshot"
        indexes = [
            models.Index(fields=["tenant", "pillar", "-ts"]),
        ]
        ordering = ["-ts"]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.pillar}:{self.ts:%Y-%m-%d}"


class TopicRegistry(models.Model):
    """Curated catalogue of topics within each pillar.

    Canonical topics are seeded per pillar. The assistant may propose new
    topics (status=proposed) when observing patterns that don't match an
    existing topic; ops promotes them to canonical or merges into an existing
    canonical via aliases.
    """

    class Status(models.TextChoices):
        CANONICAL = "canonical", "Canonical"
        PROPOSED = "proposed", "Proposed"
        DEPRECATED = "deprecated", "Deprecated"

    class Source(models.TextChoices):
        SEED = "seed", "Seed"
        PROPOSED_BY_MODEL = "proposed_by_model", "Proposed by model"
        PROMOTED = "promoted", "Promoted from proposal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pillar = models.CharField(max_length=32, choices=Pillar.choices)
    slug = models.SlugField(max_length=64)
    display_name = models.CharField(max_length=128)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CANONICAL)
    parent_topic = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    description = models.TextField(blank=True, default="")
    source = models.CharField(max_length=24, choices=Source.choices, default=Source.SEED)
    proposed_by_model_version = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    promoted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "insights_topic_registry"
        unique_together = [("pillar", "slug")]
        indexes = [
            models.Index(fields=["pillar", "status"]),
        ]
        ordering = ["pillar", "slug"]

    def __str__(self) -> str:
        return f"{self.pillar}:{self.slug}"


class TopicAlias(models.Model):
    """Synonyms that map to a canonical topic.

    Resolution path: exact slug → exact alias (case-insensitive) → (future)
    embedding similarity → propose new. Aliases keep "eating out" / "restaurants"
    collapsing into the same canonical "dining" so confidence scores aren't
    fragmented across synonyms.
    """

    class Source(models.TextChoices):
        SEED = "seed", "Seed"
        MODEL = "model", "Model"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic = models.ForeignKey(TopicRegistry, on_delete=models.CASCADE, related_name="aliases")
    alias = models.CharField(max_length=128)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.SEED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "insights_topic_alias"
        unique_together = [("topic", "alias")]
        indexes = [
            models.Index(fields=["alias"]),
        ]

    def __str__(self) -> str:
        return f"{self.topic.slug}<-{self.alias}"


class AssistantInsight(models.Model):
    """Memory of patterns the assistant has noticed about a tenant.

    Load-bearing: this is what makes the assistant→user relationship compound
    across conversations. Refuted entries are kept (the assistant needs to
    remember it was wrong) and surface in audit views.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CONFIRMED = "confirmed", "Confirmed"
        REFUTED = "refuted", "Refuted"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="assistant_insights")
    pillar = models.CharField(max_length=32, choices=Pillar.choices)
    topic = models.ForeignKey(TopicRegistry, on_delete=models.PROTECT, related_name="insights")
    statement = models.TextField()
    evidence_refs = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(default=0.0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    last_confirmed_at = models.DateTimeField(null=True, blank=True)
    last_refuted_at = models.DateTimeField(null=True, blank=True)
    user_responses = models.JSONField(default=list, blank=True)
    author_model_version = models.CharField(max_length=128, blank=True, default="")

    class Meta:
        db_table = "insights_assistant_insight"
        indexes = [
            models.Index(fields=["tenant", "pillar", "status"]),
            models.Index(fields=["tenant", "topic"]),
            models.Index(fields=["tenant", "-created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.topic_id}:{self.status}"


class UserVoicePref(models.Model):
    """Per-tenant, optionally per-topic, voice/volume overrides.

    ``register_offset`` shifts the assistant's confidence band: ``+1`` from
    "just tell me", ``-1`` from "be more cautious". When ``topic`` is null
    the preference applies pillar-wide.
    """

    class Tone(models.TextChoices):
        GENTLE = "gentle", "Gentle"
        DIRECT = "direct", "Direct"

    class Volume(models.TextChoices):
        SILENT = "silent", "Silent"
        WEEKLY = "weekly", "Weekly"
        LIVE = "live", "Live"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="voice_prefs")
    pillar = models.CharField(max_length=32, choices=Pillar.choices)
    topic = models.ForeignKey(
        TopicRegistry,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="voice_prefs",
    )
    register_offset = models.IntegerField(default=0)
    tone = models.CharField(max_length=16, choices=Tone.choices, default=Tone.GENTLE)
    volume = models.CharField(max_length=16, choices=Volume.choices, default=Volume.WEEKLY)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "insights_user_voice_pref"
        unique_together = [("tenant", "pillar", "topic")]
        indexes = [
            models.Index(fields=["tenant", "pillar"]),
        ]

    def __str__(self) -> str:
        scope = self.topic.slug if self.topic else "*"
        return f"{self.tenant_id}:{self.pillar}:{scope}"
