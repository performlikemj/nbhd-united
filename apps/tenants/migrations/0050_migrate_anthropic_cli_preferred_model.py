"""Migrate any tenant whose preferred_model is stuck on the inert
`anthropic-cli/claude-sonnet-4-6` value PR #419 shipped.

That prefix has no entry in OpenClaw 2026.4.25's model registry, so the
runtime falls back to MiniMax. PR #421 reverts to the canonical
`anthropic/<model>` form; this one-shot migration realigns any rows that
selected the broken value before the revert deployed.

No-op when no matching rows exist (fresh DBs, pre-PR-419 tenants).
"""

from django.db import migrations


def migrate_preferred_model(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(preferred_model="anthropic-cli/claude-sonnet-4-6").update(
        preferred_model="anthropic/claude-sonnet-4-6"
    )


def noop_reverse(apps, schema_editor):
    # Don't restore the broken value on rollback.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0049_tenant_byo_models_enabled"),
    ]

    operations = [
        migrations.RunPython(migrate_preferred_model, noop_reverse),
    ]
