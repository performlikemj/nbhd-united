"""Raise monthly_token_budget default from 500k to 2M and update existing tenants."""

from django.db import migrations, models


def raise_existing_budgets(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(monthly_token_budget=500_000).update(
        monthly_token_budget=2_000_000
    )


def lower_existing_budgets(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(monthly_token_budget=2_000_000).update(
        monthly_token_budget=500_000
    )


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0022_add_user_location_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tenant",
            name="monthly_token_budget",
            field=models.IntegerField(
                default=2_000_000,
                help_text="Per-user monthly token budget",
            ),
        ),
        migrations.RunPython(
            raise_existing_budgets,
            reverse_code=lower_existing_budgets,
        ),
    ]
