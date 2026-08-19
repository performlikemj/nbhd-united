"""Re-lock the public schema after tool_contract_events is created.

New public tables land with RLS off, which trips the anon-API lockdown posture
pinned by apps/tenants/test_public_schema_lockdown.py (see
0058_lock_down_public_schema_rls). RLS bare — enabled, no policies — leaves the
owning app role free to read and write (owners bypass RLS absent FORCE), which is
what aggregate telemetry queries need, while the Supabase Data API roles see
nothing.
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

REVERSE_SQL = "-- Reversing would leave the telemetry table unlocked at migration time. Do not auto-reverse."


class Migration(migrations.Migration):
    dependencies = [
        ("platform_logs", "0002_toolcontractevent"),
        ("tenants", "0156_relock_after_datebook_approval_ux"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
