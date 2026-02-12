from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0005_remove_celery_beat_schedules"),
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
