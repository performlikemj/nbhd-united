from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0131_tenant_tour_guide_enabled_tenant_tour_guide_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="journal_shaping_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
