from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenants", "0165_alter_tenant_openclaw_version")]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="mood_context_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Include the latest mood self-report in assistant context; canary opt-in.",
            ),
        ),
    ]
