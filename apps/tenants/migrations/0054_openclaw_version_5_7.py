from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0053_tenant_applied_model"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tenant",
            name="openclaw_version",
            field=models.CharField(
                default="2026.5.7",
                help_text="OpenClaw runtime version pinned to this tenant's config",
                max_length=20,
            ),
        ),
    ]
