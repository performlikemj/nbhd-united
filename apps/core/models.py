"""Core models — mindfulness profile and generated guided meditations.

The Core pillar composes a personalized ~10-minute guided meditation on demand
(the assistant authors a render *manifest*; a backend pipeline voices it via TTS
and stitches in the silences). MeditationSession stores the manifest + the
rendered audio location; audio bytes live on the per-tenant Azure File Share,
never on SQLite (fleet-corruption invariant).
"""

import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.tenants.models import Tenant


class CoreOnboardingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    IN_PROGRESS = "in_progress", "In Progress"
    COMPLETED = "completed", "Completed"
    DECLINED = "declined", "Declined"


class MeditationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RENDERING = "rendering", "Rendering"
    READY = "ready", "Ready"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"


class MeditationFailureClass(models.TextChoices):
    NONE = "", "None"
    TRANSIENT = "transient", "Transient"
    TERMINAL = "terminal", "Terminal"


class CoreProfile(models.Model):
    """Per-tenant mindfulness profile — populated via assistant-led onboarding."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="core_profile")
    onboarding_status = models.CharField(
        max_length=16,
        choices=CoreOnboardingStatus.choices,
        default=CoreOnboardingStatus.PENDING,
    )
    preferred_voice = models.CharField(
        max_length=64,
        default="Achernar",
        help_text="Pinned TTS voice name (e.g. 'Achernar', 'Aoede').",
    )
    preferred_duration_minutes = models.IntegerField(
        default=10,
        validators=[MinValueValidator(3), MaxValueValidator(30)],
        help_text="Target meditation length in minutes.",
    )
    ambient_bed_enabled = models.BooleanField(
        default=False,
        help_text="Mix a soft ambient bed under the narration (deferred; voice-only for canary).",
    )
    daily_cron_enabled = models.BooleanField(
        default=False,
        help_text="Opt-in: auto-compose one meditation each morning (spends a render/day). Default is on-demand only.",
    )
    preferred_time = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text="Preferred sit time for the optional daily cron: morning, afternoon, evening, or empty.",
    )
    additional_context = models.TextField(
        blank=True,
        default="",
        help_text="Free-form context the user wants reflected in their meditations.",
    )
    # Encryption-at-rest Phase 3 sidecar (AAD ``enc_columns.CORE_PROFILE_ADDITIONAL_CONTEXT``) — ships DARK.
    # Shares the journal flag pair (``Tenant.encrypt_journal_writes`` / ``read_encrypted_journal``).
    additional_context_enc = models.BinaryField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_profiles"

    def __str__(self) -> str:
        return f"CoreProfile({self.tenant}, {self.onboarding_status})"


class MeditationSession(models.Model):
    """A single generated guided meditation — manifest + rendered audio."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="meditation_sessions")
    date = models.DateField(help_text="Day this meditation was composed for.")
    status = models.CharField(
        max_length=12,
        choices=MeditationStatus.choices,
        default=MeditationStatus.PENDING,
    )
    failure_class = models.CharField(
        max_length=12,
        choices=MeditationFailureClass.choices,
        default=MeditationFailureClass.NONE,
        blank=True,
    )
    attempt_count = models.PositiveSmallIntegerField(default=0)
    title = models.CharField(max_length=160, blank=True, default="")
    theme = models.TextField(
        blank=True,
        default="",
        help_text="The personalized through-line for this sit.",
    )
    # Encryption-at-rest Phase 3 sidecars — ship DARK. AAD:
    # ``enc_columns.MEDITATION_SESSION_TITLE`` / ``MEDITATION_SESSION_THEME``.
    title_enc = models.BinaryField(null=True)
    theme_enc = models.BinaryField(null=True)
    voice = models.CharField(max_length=64, blank=True, default="")
    model = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="TTS model used, e.g. gemini-2.5-flash-preview-tts.",
    )
    manifest = models.JSONField(
        default=dict,
        blank=True,
        help_text="The render manifest (phases → segments). Re-renderable; auditable.",
    )
    # Encryption-at-rest Phase 3 sidecar (AAD ``enc_columns.MEDITATION_SESSION_MANIFEST``, JSON) — ships DARK.
    # Sealed alongside ``guidance_text`` — the manifest segments carry the same narration.
    manifest_enc = models.BinaryField(null=True)
    guidance_text = models.TextField(
        blank=True,
        default="",
        help_text="Flattened narration text, for display/audit.",
    )
    # Encryption-at-rest Phase 3 sidecar (AAD ``enc_columns.MEDITATION_SESSION_GUIDANCE_TEXT``) — ships DARK.
    guidance_text_enc = models.BinaryField(null=True)
    artifact_manifest_sha256 = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        help_text="Canonical manifest SHA-256 for the uploaded audio checkpoint.",
    )
    audio_url = models.CharField(max_length=512, blank=True, default="", help_text="Public URL of the rendered mp3.")
    ogg_url = models.CharField(
        max_length=512, blank=True, default="", help_text="Public URL of the ogg/opus (LINE/Telegram voice)."
    )
    duration_ms = models.IntegerField(
        null=True,
        blank=True,
        help_text="Rendered audio length in milliseconds (LINE audio messages require it).",
    )
    ambient_bed = models.CharField(max_length=64, blank=True, default="")
    error = models.TextField(blank=True, default="", help_text="Last render error, when status=failed.")
    user_feedback = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text="Optional user signal, e.g. 'liked', 'disliked', 'skipped'.",
    )
    feedback_note = models.TextField(
        blank=True,
        default="",
        help_text="Optional free-text the user leaves about this sit (what landed, what didn't).",
    )
    # Encryption-at-rest Phase 3 sidecar (AAD ``enc_columns.MEDITATION_SESSION_FEEDBACK_NOTE``) — ships DARK.
    feedback_note_enc = models.BinaryField(null=True)
    feedback_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the user last left feedback on this sit.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_meditation_sessions"
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["tenant", "date"]),
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.title or 'Meditation'} ({self.date}, {self.status})"
