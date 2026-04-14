"""Add hibernated_at field for idle container hibernation."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0026_tier_based_token_budgets"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="hibernated_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the container was idle-hibernated. Null = running normally.",
                null=True,
            ),
        ),
    ]
