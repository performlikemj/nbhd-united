from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0024_tenant_donation_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="preferred_model",
            field=models.CharField(
                blank=True,
                default="",
                help_text="User's preferred primary model (overrides tier default when set)",
                max_length=255,
            ),
        ),
    ]
