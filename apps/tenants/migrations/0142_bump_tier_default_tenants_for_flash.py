"""Re-render tier-default tenants after the starter primary switches to Flash.

Tenants with an empty ``preferred_model`` intentionally follow the tier default.
Bump their pending config version so fleet reconciliation writes the new Flash
primary. Tenants with an explicit model selection keep that choice unchanged.

Pure data migration — no schema/table changes, so it doesn't affect the
public-schema RLS relock ordering.
"""

from django.db import migrations


def bump_tier_default_tenants(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")

    for tenant in Tenant.objects.filter(preferred_model="").iterator():
        tenant.pending_config_version = (tenant.pending_config_version or 0) + 1
        tenant.save(update_fields=["pending_config_version"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0141_migrate_deepseek_flash_0731"),
    ]

    operations = [
        migrations.RunPython(bump_tier_default_tenants, noop_reverse),
    ]
