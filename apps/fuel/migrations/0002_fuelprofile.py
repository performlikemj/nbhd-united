import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fuel", "0001_initial"),
        ("tenants", "0040_fuel_enabled"),
    ]

    operations = [
        migrations.CreateModel(
            name="FuelProfile",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "onboarding_status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("in_progress", "In Progress"),
                            ("completed", "Completed"),
                            ("declined", "Declined"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                (
                    "fitness_level",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="beginner, intermediate, or advanced",
                        max_length=16,
                    ),
                ),
                (
                    "goals",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="Fitness goals, e.g. ['strength', 'weight_loss']",
                    ),
                ),
                (
                    "limitations",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="Injuries or restrictions, e.g. ['right shoulder — rotator cuff']",
                    ),
                ),
                (
                    "equipment",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="Available equipment, e.g. ['dumbbells', 'pull_up_bar']",
                    ),
                ),
                (
                    "days_per_week",
                    models.IntegerField(
                        blank=True,
                        help_text="Preferred training days per week",
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(7),
                        ],
                    ),
                ),
                ("additional_context", models.TextField(blank=True, default="", help_text="Free-form fitness context")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fuel_profile",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "fuel_profiles",
            },
        ),
    ]
