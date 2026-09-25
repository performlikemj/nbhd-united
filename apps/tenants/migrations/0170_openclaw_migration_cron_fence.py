from django.db import migrations, models

SQL = """
-- Preserve the fence for pre-staged records from earlier command revisions.
-- A committed version already selects the file transport and needs no fence.
UPDATE tenants SET openclaw_migration_cron_fenced = true
WHERE openclaw_migration ? 'signed_prestaged'
  AND COALESCE(openclaw_migration->>'status', '') <> 'PASS'
  AND openclaw_version <> '2026.9.4';

CREATE FUNCTION nbhd_migration_cron_guard(tenant_key uuid) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE blocked boolean; active boolean;
BEGIN
    SELECT openclaw_migration_cron_fenced,
           openclaw_migration <> '{}'::jsonb
           AND COALESCE(openclaw_migration->>'status', '') <> 'PASS'
      INTO blocked, active FROM tenants WHERE id = tenant_key FOR UPDATE;
    IF blocked AND active THEN
        RAISE EXCEPTION 'assistant_updating: Please try changing your reminders again in one minute.'
            USING ERRCODE = 'P0094';
    END IF;
    RETURN COALESCE(active, false);
END;
$$;
CREATE FUNCTION nbhd_migration_cron_row_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        PERFORM nbhd_migration_cron_guard(OLD.tenant_id);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        PERFORM nbhd_migration_cron_guard(NEW.tenant_id);
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$;
CREATE TRIGGER nbhd_migration_cron_fence BEFORE INSERT OR UPDATE OR DELETE
ON cron_cronjob FOR EACH ROW EXECUTE FUNCTION nbhd_migration_cron_row_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("tenants", "0169_relock_after_openclaw_migration"), ("cron", "0006_alter_cronjob_pattern")]
    operations = [
        migrations.AddField(
            model_name="tenant",
            name="openclaw_migration_cron_fenced",
            field=models.BooleanField(default=False, db_index=True, editable=False),
        ),
        migrations.RunSQL(
            SQL,
            """
            DROP TRIGGER nbhd_migration_cron_fence ON cron_cronjob;
            DROP FUNCTION nbhd_migration_cron_row_guard();
            DROP FUNCTION nbhd_migration_cron_guard(uuid);
        """,
        ),
    ]
