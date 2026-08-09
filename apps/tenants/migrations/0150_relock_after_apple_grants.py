"""Migration-time RLS hygiene after adding the Apple grants table.

``tenants.0148`` creates ``apple_grants``. A topology shift can place an older
relock before newly-created public tables, so this migration closes the expand
series and re-enables RLS on every owned table still missing it.

This is migration-time hygiene ONLY. At application startup,
``apps/tenants/management/commands/disable_rls.py`` disables RLS again for
all non-friends-backstop tables. This migration does not claim runtime
protection and deliberately adds no policies.
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

REVERSE_SQL = "-- Reversing would leave the Apple grants table unlocked at migration time. Do not auto-reverse."


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0149_backfill_apple_grants_and_outbox"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
