from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0028_tenant_preferred_model"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="monthly_cost_budget",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Monthly API cost cap in USD. 0 = use tier default.",
                max_digits=10,
            ),
        ),
    ]
