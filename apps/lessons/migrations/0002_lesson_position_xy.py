from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("lessons", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="lesson",
            name="position_x",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="lesson",
            name="position_y",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
