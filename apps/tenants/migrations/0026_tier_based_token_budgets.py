"""Reset monthly_token_budget to 0 (= use tier default) for all tenants."""

from django.db import migrations, models


def reset_to_tier_default(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.all().update(monthly_token_budget=0)


def revert_to_flat(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.all().update(monthly_token_budget=2_000_000)


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0025_alter_tenant_onboarding_complete"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tenant",
            name="monthly_token_budget",
            field=models.IntegerField(
                default=0,
                help_text="Per-user monthly token budget (0 = use tier default)",
            ),
        ),
        migrations.RunPython(reset_to_tier_default, revert_to_flat),
    ]
