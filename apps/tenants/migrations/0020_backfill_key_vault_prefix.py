"""Backfill key_vault_prefix for tenants created before the field existed."""

from django.db import migrations


def backfill(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    for tenant in Tenant.objects.filter(key_vault_prefix="").iterator():
        tenant.key_vault_prefix = f"tenants-{tenant.user_id}"
        tenant.save(update_fields=["key_vault_prefix"])


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0019_line_fields_and_link_token"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
