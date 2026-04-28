"""Fuel models — workout tracking and body-weight logging."""

import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.tenants.models import Tenant


class WorkoutCategory(models.TextChoices):
    STRENGTH = "strength", "Strength"
    CARDIO = "cardio", "Cardio"
    HIIT = "hiit", "HIIT"
    CALISTHENICS = "calisthenics", "Calisthenics"
    MOBILITY = "mobility", "Mobility"
    SPORT = "sport", "Sport"
    OTHER = "other", "Other"


class WorkoutStatus(models.TextChoices):
    DONE = "done", "Done"
    PLANNED = "planned", "Planned"
    REST = "rest", "Rest"
    IN_PROGRESS = "in_progress", "In Progress"
    SKIPPED = "skipped", "Skipped"
    RESCHEDULED = "rescheduled", "Rescheduled"


class WorkoutSource(models.TextChoices):
    USER = "user", "User"
    ASSISTANT = "assistant", "Assistant"
    TEMPLATE = "template", "Template"


class PlanStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    PAUSED = "paused", "Paused"
    ARCHIVED = "archived", "Archived"


class WorkoutPlan(models.Model):
    """A named workout program that groups planned workouts."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="workout_plans")
    name = models.CharField(max_length=128, help_text="Plan name, e.g. '4-Week Strength Builder'")
    status = models.CharField(max_length=12, choices=PlanStatus.choices, default=PlanStatus.ACTIVE)
    start_date = models.DateField(help_text="First day of the plan")
    weeks = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(52)],
        help_text="Plan duration in weeks",
    )
    days_per_week = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(7)],
        help_text="Training days per week in this plan",
    )
    schedule_json = models.JSONField(
        default=dict,
        blank=True,
        help_text="Weekly template: keys are weekday indices (0=Mon..6=Sun), values are workout definitions",
    )
    notes = models.TextField(blank=True, default="", help_text="Programming notes, progression strategy")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fuel_workout_plans"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.status}, {self.weeks}w)"


class Workout(models.Model):
    """A single workout session — planned or completed."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="workouts")
    plan = models.ForeignKey(
        WorkoutPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workouts",
        help_text="The workout plan this belongs to, if any.",
    )
    date = models.DateField(help_text="Day of the workout (derived from scheduled_at when present).")
    scheduled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Scheduled time-of-day (tz-aware). When null, the workout is day-only (legacy or completed-without-time).",
    )
    window_start_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Earliest acceptable time. Defaults to scheduled_at - 2h if null.",
    )
    window_end_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Latest acceptable time. Defaults to scheduled_at + 2h if null.",
    )
    status = models.CharField(max_length=16, choices=WorkoutStatus.choices, default=WorkoutStatus.DONE)
    source = models.CharField(
        max_length=16,
        choices=WorkoutSource.choices,
        default=WorkoutSource.USER,
        help_text="Who created this session.",
    )
    original_workout = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reschedules",
        help_text="If this is a rescheduled session, points back to the original.",
    )
    skip_reason = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Reason captured when status=skipped (e.g. 'traveling', 'kid sick').",
    )
    category = models.CharField(max_length=16, choices=WorkoutCategory.choices)
    activity = models.CharField(max_length=128, help_text="Free-text activity name, e.g. 'Push — Chest & Shoulders'")
    duration_minutes = models.IntegerField(null=True, blank=True)
    rpe = models.IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="Rate of perceived exertion (1-10)",
    )
    notes = models.TextField(blank=True, default="")
    notes_thread = models.JSONField(
        default=list,
        blank=True,
        help_text="Conversation thread on the session: list of {at, who, text} entries.",
    )
    detail_json = models.JSONField(
        default=dict,
        blank=True,
        help_text="Category-specific data: exercises/sets for strength, distance/pace for cardio, etc.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fuel_workouts"
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["tenant", "date"]),
            models.Index(fields=["tenant", "category"]),
            models.Index(fields=["tenant", "status", "date"]),
            models.Index(fields=["tenant", "scheduled_at"]),
            models.Index(fields=["tenant", "status", "scheduled_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.activity} ({self.category}, {self.date})"


class OnboardingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    IN_PROGRESS = "in_progress", "In Progress"
    COMPLETED = "completed", "Completed"
    DECLINED = "declined", "Declined"


class FuelProfile(models.Model):
    """Per-tenant fitness profile — populated via assistant-led onboarding."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="fuel_profile")
    onboarding_status = models.CharField(
        max_length=16,
        choices=OnboardingStatus.choices,
        default=OnboardingStatus.PENDING,
    )
    fitness_level = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text="beginner, intermediate, or advanced",
    )
    goals = models.JSONField(default=list, blank=True, help_text="Fitness goals, e.g. ['strength', 'weight_loss']")
    limitations = models.JSONField(
        default=list, blank=True, help_text="Injuries or restrictions, e.g. ['right shoulder — rotator cuff']"
    )
    equipment = models.JSONField(
        default=list, blank=True, help_text="Available equipment, e.g. ['dumbbells', 'pull_up_bar']"
    )
    days_per_week = models.IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(7)],
        help_text="Preferred training days per week",
    )
    preferred_days = models.JSONField(
        default=list,
        blank=True,
        help_text="Preferred training days as weekday indices: 0=Monday ... 6=Sunday, e.g. [0, 2, 4]",
    )
    preferred_time = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text="Preferred workout time: morning, afternoon, evening, or empty",
    )
    additional_context = models.TextField(blank=True, default="", help_text="Free-form fitness context")
    use_session_scheduling = models.BooleanField(
        default=False,
        help_text=(
            "Cutover flag for the per-session Fuel cron model: when True, the "
            "tenant's _fuel:* crons are derived from Workout.scheduled_at and "
            "the legacy preferred_time-based emission is suppressed."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fuel_profiles"

    def __str__(self) -> str:
        return f"FuelProfile({self.tenant}, {self.onboarding_status})"


class WorkoutTemplate(models.Model):
    """Reusable workout template for quick logging."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="workout_templates")
    name = models.CharField(max_length=128, help_text="Template name, e.g. 'Push Day A'")
    category = models.CharField(max_length=16, choices=WorkoutCategory.choices)
    activity = models.CharField(max_length=128)
    duration_minutes = models.IntegerField(null=True, blank=True)
    detail_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fuel_workout_templates"
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.category})"


class PersonalRecord(models.Model):
    """A personal record achieved during a workout."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="personal_records")
    workout = models.ForeignKey(Workout, on_delete=models.CASCADE, related_name="prs")
    exercise_name = models.CharField(max_length=128)
    category = models.CharField(max_length=16, choices=WorkoutCategory.choices)
    value = models.DecimalField(max_digits=8, decimal_places=2, help_text="The new record value")
    previous_value = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    metric = models.CharField(max_length=16, default="est_1rm", help_text="est_1rm, distance, hold_s, reps")
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fuel_personal_records"
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["tenant", "exercise_name"]),
        ]

    def __str__(self) -> str:
        return f"PR: {self.exercise_name} {self.value} ({self.date})"


class FuelGoal(models.Model):
    """A fitness goal with a target value and optional deadline."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="fuel_goals")
    exercise_name = models.CharField(max_length=128)
    metric = models.CharField(max_length=16, default="est_1rm")
    target_value = models.DecimalField(max_digits=8, decimal_places=2)
    target_date = models.DateField(null=True, blank=True)
    achieved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fuel_goals"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Goal: {self.exercise_name} → {self.target_value}"


class RestingHeartRateLog(models.Model):
    """Daily resting heart rate entry for trend tracking."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="rhr_logs")
    date = models.DateField()
    bpm = models.IntegerField(validators=[MinValueValidator(20), MaxValueValidator(250)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fuel_resting_heart_rate"
        ordering = ["-date"]
        unique_together = ["tenant", "date"]

    def __str__(self) -> str:
        return f"{self.date} — {self.bpm} bpm"


class SleepLog(models.Model):
    """Daily sleep duration entry for trend tracking."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="sleep_logs")
    date = models.DateField()
    duration_hours = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(24)],
        help_text="Sleep duration in hours, e.g. 7.5",
    )
    quality = models.IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="Sleep quality rating 1-5 (optional)",
    )
    notes = models.TextField(blank=True, default="", help_text="Optional notes, e.g. 'woke up twice'")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fuel_sleep"
        ordering = ["-date"]
        unique_together = ["tenant", "date"]

    def __str__(self) -> str:
        return f"{self.date} — {self.duration_hours}h"


class BodyWeightLog(models.Model):
    """Daily body-weight entry for trend tracking."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="body_weight_logs")
    date = models.DateField()
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fuel_body_weight"
        ordering = ["-date"]
        unique_together = ["tenant", "date"]

    def __str__(self) -> str:
        return f"{self.date} — {self.weight_kg} kg"
