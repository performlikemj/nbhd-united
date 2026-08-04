"""Re-run migration-time public-schema RLS lockdown after OpenRouter analytics."""

from django.db import migrations

RELOCK_SQL = r"""
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT schemaname, tablename
    FROM pg_tables
    WHERE schemaname = 'public'
      AND tableowner = current_user
      AND rowsecurity = false
  LOOP
    EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
                   r.schemaname, r.tablename);
  END LOOP;
END
$$;
"""

REVERSE_SQL = "-- Do not auto-reverse; this would leave public tables unlocked at migration time."


class Migration(migrations.Migration):
    dependencies = [
        ("steward", "0015_openrouter_model_daily"),
        ("tenants", "0142_bump_tier_default_tenants_for_flash"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
