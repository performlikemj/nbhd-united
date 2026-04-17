from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0029_tenant_monthly_cost_budget"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="task_model_preferences",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Per-task model overrides. Keys: heartbeat, morning_briefing, "
                    "evening_checkin, week_review, background_tasks. "
                    "Values: model IDs."
                ),
            ),
        ),
    ]
