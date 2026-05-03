"""Add ``delivery_in_flight_until`` lease column so concurrent QStash
retries of ``deliver_buffered_messages_task`` don't fire duplicate
``/v1/chat/completions`` requests at the container while the first
attempt is still running.

Backfills as NULL (no lease held) so existing rows are immediately
deliverable on the next task run.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("router", "0002_buffered_message_delivery_attempts"),
    ]

    operations = [
        migrations.AddField(
            model_name="bufferedmessage",
            name="delivery_in_flight_until",
            field=models.DateTimeField(
                null=True,
                blank=True,
                help_text=(
                    "Soft lease: while now() < this timestamp, an in-progress "
                    "deliver_buffered_messages task is mid-POST for this row. "
                    "Concurrent QStash retries skip rows whose lease is still "
                    "live so we don't fire duplicate /v1/chat/completions calls "
                    "at the container while the first turn is still running."
                ),
            ),
        ),
    ]
