from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0031_userllmconfig_multi_provider"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tenant",
            name="model_tier",
            field=models.CharField(
                choices=[
                    ("starter", "Starter (MiniMax M2.7)"),
                    ("premium", "Premium (Sonnet/Opus)"),
                    ("byok", "Bring Your Own Key"),
                ],
                default="starter",
                max_length=20,
            ),
        ),
    ]
