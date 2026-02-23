"""Backfill onboarding_complete=True for all existing active tenants.

Prevents existing users from being forced through the onboarding flow
after the onboarding feature is deployed.
"""
from django.db import migrations


def backfill_onboarding(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    updated = Tenant.objects.filter(
        status="active",
        onboarding_complete=False,
    ).update(onboarding_complete=True, onboarding_step=4)
    if updated:
        print(f"\n  Backfilled onboarding_complete=True for {updated} existing tenant(s)")


def reverse_backfill(apps, schema_editor):
    pass  # No meaningful reverse — can't distinguish pre-existing from newly onboarded


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0014_onboarding_fields"),
    ]

    operations = [
        migrations.RunPython(backfill_onboarding, reverse_backfill),
    ]
