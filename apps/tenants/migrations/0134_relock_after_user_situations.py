"""Re-run the public-schema RLS lockdown after adding ``user_situations``.

``tenants.0133_situational_context`` creates a new public table. Adding it can
re-sort the global migration graph so an earlier relock runs before every table
exists. Depend on the create migration and re-enable RLS at the new graph tail.
The SQL is idempotent and adds no policy; it preserves the anon Supabase Data
API lockdown tested by ``apps.tenants.test_public_schema_lockdown``.

As with the other control-plane relocks, this is a migration-time posture. The
boot-time ``disable_rls`` sweep still leaves Django as the runtime tenant
boundary for tables outside its explicit keep-set.
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

REVERSE_SQL = "-- Do not auto-reverse; would leave public tables unlocked at migration time."


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0133_situational_context"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
