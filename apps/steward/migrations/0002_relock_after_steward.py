"""Re-run the public-schema migration-time lockdown after Steward tables.

The platform disables RLS at runtime for portfolio tables; this migration only
preserves the repo-wide migration-time invariant that every newly created
owned public table has RLS enabled before the lockdown test runs.
"""

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
        ("steward", "0001_initial"),
        ("tenants", "0138_relock_after_yardtalk"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
