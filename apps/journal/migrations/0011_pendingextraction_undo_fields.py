"""Add lesson_id and UNDONE status to PendingExtraction for auto-add + undo flow."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("journal", "0010_remove_evening_from_template"),
    ]

    operations = [
        migrations.AddField(
            model_name="pendingextraction",
            name="lesson_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="pendingextraction",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("approved", "Approved"),
                    ("dismissed", "Dismissed"),
                    ("expired", "Expired"),
                    ("undone", "Undone"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
    ]
