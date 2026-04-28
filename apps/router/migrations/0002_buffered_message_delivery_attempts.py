"""Add delivery_attempts + delivery_status to BufferedMessage so the
buffered-delivery task can drop messages past an attempt cap instead
of head-of-line-blocking the queue forever."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("router", "0001_buffered_message"),
    ]

    operations = [
        migrations.AddField(
            model_name="bufferedmessage",
            name="delivery_attempts",
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text=("Number of times deliver_buffered_messages has tried and failed to deliver this message."),
            ),
        ),
        migrations.AddField(
            model_name="bufferedmessage",
            name="delivery_status",
            field=models.CharField(
                max_length=16,
                choices=[
                    ("pending", "Pending"),
                    ("delivered", "Delivered"),
                    ("failed", "Failed"),
                ],
                default="pending",
                help_text=("Terminal state: 'delivered' on success, 'failed' after attempts cap reached."),
            ),
        ),
    ]
