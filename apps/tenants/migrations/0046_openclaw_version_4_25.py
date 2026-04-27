from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0045_openclaw_version_4_21"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tenant",
            name="openclaw_version",
            field=models.CharField(
                default="2026.4.25",
                help_text="OpenClaw runtime version pinned to this tenant's config",
                max_length=20,
            ),
        ),
    ]
