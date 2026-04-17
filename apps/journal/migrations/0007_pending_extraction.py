"""Add PendingExtraction model for nightly goal/task/lesson extraction flow."""

import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0006_rename_journal_doc_tenant_kind_idx_journal_doc_tenant__efec3b_idx"),
        ("tenants", "0008_add_userllmconfig"),
    ]

    operations = [
        migrations.CreateModel(
            name="PendingExtraction",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pending_extractions",
                        to="tenants.tenant",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("lesson", "Lesson"), ("goal", "Goal"), ("task", "Task")],
                        max_length=16,
                    ),
                ),
                ("text", models.TextField()),
                ("tags", models.JSONField(default=list)),
                ("confidence", models.CharField(default="medium", max_length=8)),
                ("source_date", models.DateField(blank=True, null=True)),
                ("expires_at", models.DateTimeField()),
                ("telegram_message_id", models.CharField(blank=True, max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("dismissed", "Dismissed"),
                            ("expired", "Expired"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "journal_pending_extractions",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["tenant", "status"], name="journal_pen_tenant__a1d532_idx"),
                    models.Index(fields=["tenant", "kind", "status"], name="journal_pen_tenant__44d381_idx"),
                    models.Index(fields=["expires_at"], name="journal_pen_expires_396cb6_idx"),
                ],
            },
        ),
    ]
