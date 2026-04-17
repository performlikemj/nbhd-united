"""Unify gmail + google-calendar providers into single 'google' provider.

Migrates existing Integration records and updates the Provider choices.
"""

from django.db import migrations, models


def merge_google_providers(apps, schema_editor):
    """Rename gmail/google-calendar integrations to 'google'.

    If a tenant has both, keep the gmail one (it has the broader tokens)
    and delete the google-calendar record to avoid unique constraint violation.
    """
    Integration = apps.get_model("integrations", "Integration")

    # First, delete google-calendar records where tenant also has gmail
    gmail_tenant_ids = set(Integration.objects.filter(provider="gmail").values_list("tenant_id", flat=True))
    Integration.objects.filter(
        provider="google-calendar",
        tenant_id__in=gmail_tenant_ids,
    ).delete()

    # Rename remaining google-calendar → google
    Integration.objects.filter(provider="google-calendar").update(provider="google")

    # Rename gmail → google
    Integration.objects.filter(provider="gmail").update(provider="google")


def reverse_merge(apps, schema_editor):
    """Reverse: rename 'google' back to 'gmail'."""
    Integration = apps.get_model("integrations", "Integration")
    Integration.objects.filter(provider="google").update(provider="gmail")


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0005_reddit_provider"),
    ]

    operations = [
        migrations.RunPython(merge_google_providers, reverse_merge),
        migrations.AlterField(
            model_name="integration",
            name="provider",
            field=models.CharField(
                choices=[
                    ("google", "Google Workspace"),
                    ("sautai", "Sautai"),
                    ("reddit", "Reddit"),
                ],
                max_length=50,
            ),
        ),
    ]
