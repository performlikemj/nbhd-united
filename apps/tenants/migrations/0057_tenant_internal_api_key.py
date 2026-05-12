"""Add Tenant.internal_api_key for per-tenant internal-auth migration."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0056_agenda_commitment"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="internal_api_key",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Per-tenant secret used by the tenant's OpenClaw container to "
                    "authenticate to Django internal endpoints. Stored raw; Container "
                    "Apps secret reference (kv-nbhd-prod/secrets/tenant-<uuid>-internal-key) "
                    "is the runtime source of truth for the container side."
                ),
                max_length=128,
            ),
        ),
    ]
