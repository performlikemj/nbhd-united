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


class Workout(models.Model):
    """A single workout session — planned or completed."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="workouts")
    date = models.DateField()
    status = models.CharField(max_length=10, choices=WorkoutStatus.choices, default=WorkoutStatus.DONE)
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
        ]

    def __str__(self) -> str:
        return f"{self.activity} ({self.category}, {self.date})"


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
