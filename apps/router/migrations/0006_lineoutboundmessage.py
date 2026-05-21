import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("router", "0005_processedinboundevent"),
        ("tenants", "0067_postgres_cron_canonical_default_true"),
    ]

    operations = [
        migrations.CreateModel(
            name="LineOutboundMessage",
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
                ("line_user_id", models.CharField(max_length=128)),
                (
                    "line_message_id",
                    models.CharField(
                        help_text="ID returned by LINE's push/reply API in sentMessages[].id.",
                        max_length=64,
                        unique=True,
                    ),
                ),
                (
                    "text_excerpt",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="First ~500 chars of the message we sent — used as the quoted excerpt.",
                    ),
                ),
                ("sent_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="line_outbound_messages",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "line_outbound_messages",
                "ordering": ["-sent_at"],
                "indexes": [
                    models.Index(
                        fields=["tenant", "line_user_id", "-sent_at"],
                        name="line_outb_tenant_user_idx",
                    ),
                ],
            },
        ),
    ]
