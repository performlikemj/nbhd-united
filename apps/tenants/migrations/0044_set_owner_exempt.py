"""Set platform owner's tenant as budget-exempt."""

from django.db import migrations


def set_owner_exempt(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(user__email="mj@bywayofmj.com").update(is_budget_exempt=True)


def unset_owner_exempt(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(user__email="mj@bywayofmj.com").update(is_budget_exempt=False)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0043_tenant_is_budget_exempt"),
    ]

    operations = [
        migrations.RunPython(set_owner_exempt, unset_owner_exempt),
    ]
