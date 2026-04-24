"""Set platform owner's tenant as budget-exempt."""

import os

from django.db import migrations


def set_owner_exempt(apps, schema_editor):
    owner_email = os.environ.get("PLATFORM_OWNER_EMAIL", "")
    if not owner_email:
        return
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(user__email=owner_email).update(is_budget_exempt=True)


def unset_owner_exempt(apps, schema_editor):
    owner_email = os.environ.get("PLATFORM_OWNER_EMAIL", "")
    if not owner_email:
        return
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(user__email=owner_email).update(is_budget_exempt=False)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0043_tenant_is_budget_exempt"),
    ]

    operations = [
        migrations.RunPython(set_owner_exempt, unset_owner_exempt),
    ]
