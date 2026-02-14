from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0006_tenant_internal_api_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="timezone",
            field=models.CharField(
                default="UTC",
                help_text="IANA timezone string, e.g. 'America/New_York'",
                max_length=63,
            ),
        ),
    ]
