"""Re-run public-schema migration-time lockdown after Steward Phase 2b tables."""

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
        ("steward", "0008_alter_evidenceevent_source_and_more"),
        ("tenants", "0140_relock_after_apple_auth"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
