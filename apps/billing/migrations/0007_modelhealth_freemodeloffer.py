from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0006_add_is_system_event"),
    ]

    operations = [
        migrations.CreateModel(
            name="ModelHealth",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "model_id",
                    models.CharField(
                        help_text="Canonical OpenClaw-form model id, e.g. openrouter/deepseek/deepseek-v4-pro",
                        max_length=255,
                        unique=True,
                    ),
                ),
                ("is_reachable", models.BooleanField(default=True)),
                (
                    "is_free",
                    models.BooleanField(
                        default=False,
                        help_text="True when OpenRouter reports prompt + completion price == 0.",
                    ),
                ),
                ("consecutive_failures", models.IntegerField(default=0)),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("last_ok_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, default="", max_length=500)),
                (
                    "pricing",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Last-seen OpenRouter pricing block for this model.",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "model_health",
            },
        ),
        migrations.CreateModel(
            name="FreeModelOffer",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False),
                ),
                (
                    "model_id",
                    models.CharField(
                        default="openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
                        help_text="The model offered for free while the promo runs.",
                        max_length=255,
                    ),
                ),
                (
                    "fallback_model_id",
                    models.CharField(
                        default="openrouter/deepseek/deepseek-v4-pro",
                        help_text="Model tenants fall back to when the promo is not active.",
                        max_length=255,
                    ),
                ),
                (
                    "enabled",
                    models.BooleanField(
                        default=True,
                        help_text="Operator kill-switch. When False the promo never activates regardless of health.",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=False,
                        help_text="Whether the free model is currently the advertised default. Driven by the health cron.",
                    ),
                ),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("deactivated_at", models.DateTimeField(blank=True, null=True)),
                ("last_transition_reason", models.CharField(blank=True, default="", max_length=255)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "free_model_offer",
            },
        ),
    ]
