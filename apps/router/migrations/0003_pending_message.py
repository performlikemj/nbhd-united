"""Create the ``pending_messages`` table that powers per-tenant
serialization of warm-tenant messages.

Distinct from ``BufferedMessage`` (hibernation buffer): this queue exists
to prevent the OpenClaw claude-cli backend from receiving overlapping
turns on the same live session. Each row carries the prepared payload
(workspace + datetime markers already injected) plus enough context for
the drain task to relay the response back to the user via the originating
channel.

The ``delivery_in_flight_until`` lease mirrors PR #430's pattern for
``BufferedMessage``: a concurrent QStash retry / cron tick observes the
live lease and skips the row instead of firing a duplicate
``/v1/chat/completions`` while the first turn is mid-flight.
"""

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("router", "0002_buffered_message_delivery_attempts"),
        ("tenants", "0027_tenant_hibernated_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="PendingMessage",
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
                    "channel_user_id",
                    models.CharField(
                        help_text=(
                            "Per-channel user identifier (line_user_id for LINE, "
                            "Telegram chat_id stringified for Telegram). Used to "
                            "scope the queue so two distinct users on the same "
                            "tenant don't block each other."
                        ),
                        max_length=128,
                    ),
                ),
                (
                    "payload",
                    models.JSONField(
                        help_text=(
                            "Channel-specific bundle with everything the drain "
                            "task needs to forward the message and relay the "
                            "reply: prepared message_text (with workspace + "
                            "datetime markers already injected), user_param, "
                            "user_timezone, reply_token, is_voice, etc."
                        ),
                    ),
                ),
                (
                    "user_text",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Raw user-facing excerpt for logging (truncated).",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                (
                    "delivery_status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("delivered", "Delivered"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        help_text=("Terminal state: 'delivered' on success, 'failed' after attempts cap reached."),
                        max_length=16,
                    ),
                ),
                (
                    "delivery_attempts",
                    models.PositiveSmallIntegerField(
                        default=0,
                        help_text=("Number of times the drain task has tried and failed to deliver this message."),
                    ),
                ),
                (
                    "delivery_in_flight_until",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        help_text=(
                            "Soft lease: while now() < this timestamp, an "
                            "in-progress drain task is mid-POST for this row. "
                            "Concurrent QStash retries skip rows whose lease is "
                            "still live so we don't fire duplicate "
                            "/v1/chat/completions calls at the container while "
                            "the first turn is still running."
                        ),
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pending_messages",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "pending_messages",
                "ordering": ["created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="pendingmessage",
            index=models.Index(
                fields=[
                    "tenant",
                    "channel",
                    "channel_user_id",
                    "delivery_status",
                    "created_at",
                ],
                name="pmsg_drain_lookup_idx",
            ),
        ),
    ]
