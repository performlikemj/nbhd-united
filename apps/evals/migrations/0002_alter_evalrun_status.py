from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("evals", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="evalrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("running", "Running"),
                    ("pass", "Pass"),
                    ("degraded", "Degraded"),
                    ("fail", "Fail"),
                    ("error", "Error"),
                ],
                default="running",
                max_length=16,
            ),
        ),
    ]
