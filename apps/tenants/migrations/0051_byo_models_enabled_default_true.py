"""Flip ``byo_models_enabled`` to True for the entire fleet.

Phase 1 launched with the flag default-off and a per-tenant
``enable_byo --tenant <id>`` command for canary rollout. After validating
on `oc-148ccf1c` (PRs #421-#432), the product owner authorized fleet-wide
availability.

This migration:

  1. Bulk-updates every non-deleted tenant row to ``byo_models_enabled=True``.
     Active, hibernated, and suspended tenants all get the flag — only
     ``Status.DELETED`` rows are skipped (their containers are gone).
  2. Idempotent: running twice is a no-op (the second pass updates rows
     already at ``True``).
  3. Reverse migration sets every row back to False so a rollback puts
     the fleet in a consistent pre-Phase-2 state. (Hibernated tenants
     pick up the flag change on their next wake / config push.)

The schema-level default flip lives in ``apps.tenants.models.Tenant`` —
this migration only handles existing rows.
"""

from django.db import migrations, models


def enable_byo_for_fleet(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.exclude(status="deleted").update(byo_models_enabled=True)


def disable_byo_for_fleet(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.update(byo_models_enabled=False)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0050_migrate_anthropic_cli_preferred_model"),
    ]

    operations = [
        # Step 1: backfill existing rows (active/hibernated/suspended).
        migrations.RunPython(enable_byo_for_fleet, disable_byo_for_fleet),
        # Step 2: align the column default with the new fleet-wide policy
        # so newly provisioned tenants are auto-enabled.
        migrations.AlterField(
            model_name="tenant",
            name="byo_models_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Enable bring-your-own Anthropic/OpenAI subscription mode for this tenant",
            ),
        ),
    ]
