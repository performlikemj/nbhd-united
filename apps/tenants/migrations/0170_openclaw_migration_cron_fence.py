from django.db import migrations, models

SQL = """
-- Preserve the fence for pre-staged records from earlier command revisions.
-- A committed version already selects the file transport and needs no fence.
UPDATE tenants SET openclaw_migration_cron_fenced = true
WHERE openclaw_migration ? 'signed_prestaged'
  AND COALESCE(openclaw_migration->>'status', '') <> 'PASS'
  AND openclaw_version <> '2026.9.4';
"""


class Migration(migrations.Migration):
    dependencies = [("tenants", "0169_relock_after_openclaw_migration"), ("cron", "0006_alter_cronjob_pattern")]
    operations = [
        migrations.AddField(
            model_name="tenant",
            name="openclaw_migration_cron_fenced",
            field=models.BooleanField(default=False, db_index=True, editable=False),
        ),
        migrations.RunSQL(SQL, migrations.RunSQL.noop),
    ]
