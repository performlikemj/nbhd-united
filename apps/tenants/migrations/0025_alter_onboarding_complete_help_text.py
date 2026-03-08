from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0024_tenant_donation_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tenant",
            name="onboarding_complete",
            field=models.BooleanField(
                default=False,
                help_text="Whether messaging onboarding has been completed",
            ),
        ),
    ]
