from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0041_merge_0040_pat_and_fuel"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="cron_wake_at",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "When the container was woken for a scheduled cron job. "
                    "Null = not a cron wake. Used to apply the shorter 30-min idle window."
                ),
                null=True,
            ),
        ),
    ]
