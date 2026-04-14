"""Add Reddit to Integration.Provider choices.

Choices-only migration — no DB schema change required, but included for
completeness so the migration history reflects the model state.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0004_composio_connected_account_id"),
    ]

    operations = [
        migrations.AlterField(
            model_name="integration",
            name="provider",
            field=__import__("django.db.models", fromlist=["CharField"]).CharField(
                choices=[
                    ("gmail", "Gmail"),
                    ("google-calendar", "Google Calendar"),
                    ("sautai", "Sautai"),
                    ("reddit", "Reddit"),
                ],
                max_length=50,
            ),
        ),
    ]
