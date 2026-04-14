from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0032_update_starter_tier_label"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="finance_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Enable budget tracking and debt payoff tools",
            ),
        ),
    ]
