from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0035_feature_tips_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="cron_jobs_snapshot",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Last-known cron job list from gateway. Format: {"jobs": [...], "snapshot_at": "ISO8601"}',
            ),
        ),
    ]
