import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0030_tenant_task_model_preferences"),
    ]

    operations = [
        # 1. Change OneToOneField → ForeignKey
        migrations.AlterField(
            model_name="userllmconfig",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_configs",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # 2. Add unique constraint on (user, provider)
        migrations.AddConstraint(
            model_name="userllmconfig",
            constraint=models.UniqueConstraint(
                fields=["user", "provider"],
                name="unique_user_provider",
            ),
        ),
    ]
