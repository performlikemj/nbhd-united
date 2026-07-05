"""Re-run the public-schema RLS lockdown after adding the friends edge tables.

PR0 of the Neighborhood layer adds three public tables (``neighbor_profiles``,
``friendships``, ``friend_invites`` via ``friends.0001``). Per the recurring
hazard (see ``0066``, ``0073``, ``0078``, ``0080``, ``0083``), any new
migration can push a prior relock earlier in the topo sort, leaving newer
tables without RLS — and ``apps.tenants.test_public_schema_lockdown
.test_rls_enabled_on_owned_public_tables`` fails the build when an owned
public table has ``rowsecurity = false``.

This relock depends on ``friends.0001`` (which creates the tables) and the
current latest ``tenants`` migration so it runs after both, re-locking the new
tables AND anything else that escaped a previous relock. SQL is idempotent
(ENABLE ROW LEVEL SECURITY on an already-RLS table is a no-op) and adds NO
policy (``test_no_policies_on_public_schema`` forbids policies on ``public.*``;
Django is BYPASSRLS, so the relock protects the anon Supabase Data API, not
tenant isolation — that is the accessor's job in apps/friends/access.py).
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

REVERSE_SQL = (
    "-- Reversing this migration would leave the friends edge tables without "
    "RLS, re-exposing them via PostgREST/anon. Do not auto-reverse."
)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0096_tenant_friends_enabled"),
        ("friends", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
