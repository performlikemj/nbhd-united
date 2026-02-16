"""Create Document model and migrate existing data."""
from __future__ import annotations

import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("journal", "0003_add_note_templates"),
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Document",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(
                    choices=[
                        ("daily", "Daily Note"),
                        ("weekly", "Weekly Review"),
                        ("monthly", "Monthly Review"),
                        ("goal", "Goal"),
                        ("project", "Project"),
                        ("tasks", "Tasks"),
                        ("ideas", "Ideas"),
                        ("memory", "Memory"),
                    ],
                    max_length=32,
                )),
                ("slug", models.CharField(max_length=128)),
                ("title", models.CharField(max_length=256)),
                ("markdown", models.TextField(default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("tenant", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="documents",
                    to="tenants.tenant",
                )),
            ],
            options={
                "unique_together": {("tenant", "kind", "slug")},
                "ordering": ["-updated_at"],
                "indexes": [
                    models.Index(fields=["tenant", "kind"], name="journal_doc_tenant_kind_idx"),
                ],
            },
        ),
    ]
