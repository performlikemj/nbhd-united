"""AgendaEngagement — Phase B engagement metadata for the agenda meta-view."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0052_welcomes_sent"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgendaEngagement",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("feature_intro", "Feature introduction"),
                            ("planned_workout", "Planned workout"),
                            ("fuel_goal", "Fuel goal"),
                            ("payoff_plan", "Payoff plan"),
                            ("task", "Task (markdown)"),
                            ("goal", "Goal (markdown)"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "item_id",
                    models.CharField(
                        help_text="Stable identifier for the thread (feature key, UUID, content hash, ...)",
                        max_length=128,
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("nascent", "Not yet introduced"),
                            ("introduced", "Surfaced once, awaiting engagement"),
                            ("active", "User has engaged with this thread"),
                            ("dormant", "Was active but has gone quiet"),
                            ("abandoned", "User signaled disinterest — don't re-surface"),
                            ("completed", "Done — no longer needs surfacing"),
                        ],
                        default="nascent",
                        max_length=16,
                    ),
                ),
                (
                    "last_surfaced_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="When the assistant last proactively surfaced this thread.",
                        null=True,
                    ),
                ),
                (
                    "surface_after",
                    models.DateTimeField(
                        blank=True,
                        help_text=(
                            "Earliest acceptable next-surface time. Used to defer "
                            "threads the user pushed back on, or to honor an "
                            "explicit assistant commitment ('check in on this in 2 weeks')."
                        ),
                        null=True,
                    ),
                ),
                (
                    "response_signals",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text=(
                            "Append-only log of {at, signal} dicts. Signal vocabulary: "
                            "'warm' (engaged positively), 'redirect' (changed subject), "
                            "'ignore' (no response), 'organic' (user brought it up first)."
                        ),
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="agenda_engagements",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "indexes": [
                    # Auto-named so Django's ``makemigrations --check`` is
                    # happy in CI. Hand-coded names diverge from Django's
                    # deterministic naming and trigger spurious ``rename
                    # index`` migrations on each check.
                    models.Index(fields=["tenant", "state"]),
                    models.Index(fields=["tenant", "kind", "last_surfaced_at"]),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "kind", "item_id"),
                        name="agenda_engagement_unique_tenant_kind_item",
                    ),
                ],
            },
        ),
    ]
