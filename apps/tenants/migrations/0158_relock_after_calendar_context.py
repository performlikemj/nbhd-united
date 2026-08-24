"""Re-run the public-schema RLS lockdown after per-calendar context tables."""

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

REVERSE_SQL = "-- Reversing would leave calendar-context tables unlocked at migration time. Do not auto-reverse."


class Migration(migrations.Migration):
    dependencies = [
        ("datebook", "0005_per_calendar_context"),
        ("tenants", "0157_relock_after_tool_telemetry"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
