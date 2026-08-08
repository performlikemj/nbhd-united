"""Re-render tier-default tenants after the starter primary returns to Pro.

Tenants with an empty ``preferred_model`` intentionally follow the rolling tier
default. Bump only those tenants' pending config version so fleet reconciliation
writes the new Pro primary. A stored Flash (or any other non-empty model) is an
explicit choice and remains unchanged.

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
        ("tenants", "0145_tenant_layer1_placeholder_writes"),
    ]

    operations = [
        migrations.RunPython(bump_tier_default_tenants, noop_reverse),
    ]
