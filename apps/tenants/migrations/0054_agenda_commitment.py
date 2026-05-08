"""Phase D: ASSISTANT_COMMITMENT kind + metadata field on AgendaEngagement."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0053_agenda_engagement"),
    ]

    operations = [
        migrations.AddField(
            model_name="agendaengagement",
            name="metadata",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Per-kind extra data. For ASSISTANT_COMMITMENT: "
                    "{'about': str, 'why': str}. For other kinds: empty by "
                    "default — extension point for kind-specific fields."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="agendaengagement",
            name="kind",
            field=models.CharField(
                choices=[
                    ("feature_intro", "Feature introduction"),
                    ("planned_workout", "Planned workout"),
                    ("fuel_goal", "Fuel goal"),
                    ("payoff_plan", "Payoff plan"),
                    ("task", "Task (markdown)"),
                    ("goal", "Goal (markdown)"),
                    ("assistant_commitment", "Assistant commitment"),
                ],
                max_length=32,
            ),
        ),
    ]
