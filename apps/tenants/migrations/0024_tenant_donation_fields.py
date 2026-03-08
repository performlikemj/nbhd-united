import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0023_raise_default_token_budget"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="donation_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Opt-in to donate surplus subscription revenue",
            ),
        ),
        migrations.AddField(
            model_name="tenant",
            name="donation_percentage",
            field=models.IntegerField(
                default=100,
                help_text="Percentage of surplus to donate (0-100)",
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(100),
                ],
            ),
        ),
    ]
