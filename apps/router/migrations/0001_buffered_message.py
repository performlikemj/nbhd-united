"""Create BufferedMessage model for idle-hibernation message queuing."""

import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("router", "__first__"),
        ("tenants", "0027_tenant_hibernated_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="BufferedMessage",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "channel",
                    models.CharField(
                        choices=[("telegram", "Telegram"), ("line", "Line")],
                        max_length=16,
                    ),
                ),
                (
                    "payload",
                    models.JSONField(
                        help_text="Raw webhook payload (Telegram update or LINE event)",
                    ),
                ),
                (
                    "user_text",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Extracted user message text for logging (truncated)",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("delivered", models.BooleanField(default=False)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="buffered_messages",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "buffered_messages",
                "ordering": ["created_at"],
            },
        ),
    ]
