"""Migration-time RLS hygiene after adding the three Apple auth tables.

``tenants.0139`` creates ``apple_auth_transactions``,
``external_identities``, and ``apple_revocation_outbox``. A topology shift can
place an older relock before newly-created public tables, so this migration
depends on 0139 and re-enables RLS on every owned table still missing it.

This is migration-time hygiene ONLY. At application startup,
``apps/tenants/management/commands/disable_rls.py`` disables RLS again for
all non-friends-backstop tables, including these three. This migration does
not claim runtime protection and deliberately adds no policies.
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

REVERSE_SQL = "-- Reversing would leave the Apple auth tables unlocked at migration time. Do not auto-reverse."


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0139_apple_sign_in"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
