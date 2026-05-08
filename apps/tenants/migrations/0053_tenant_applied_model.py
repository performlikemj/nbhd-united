"""Add applied_model and applied_model_at to Tenant.

`applied_model` is stamped after a successful `gateway.reload` in
`apps.orchestrator.tasks.apply_single_tenant_config_task`. The frontend
compares it against `preferred_model` to render an honest 'Switching…'
state while a picker change is in flight.

Backfill: existing rows get `applied_model = preferred_model`. Without
this, every tenant's badge would read 'Switching…' until they next
changed models.
"""

from django.db import migrations, models


def backfill_applied_model(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(applied_model="").update(applied_model=models.F("preferred_model"))


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0052_welcomes_sent"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="applied_model",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Model the running container is currently serving, stamped "
                    "after a successful gateway.reload. Diverges from "
                    "preferred_model while a switch is in flight; the frontend "
                    "uses the difference to render a 'Switching…' state instead "
                    "of an immediate 'Active' badge."
                ),
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="tenant",
            name="applied_model_at",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "Timestamp of the last successful applied_model write. Used "
                    "by the frontend to detect 'still applying, taking longer "
                    "than usual'."
                ),
                null=True,
            ),
        ),
        migrations.RunPython(backfill_applied_model, noop_reverse),
    ]
