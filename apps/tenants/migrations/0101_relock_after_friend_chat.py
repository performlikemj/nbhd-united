"""Re-run the public-schema RLS lockdown after adding the friend-chat tables.

PR5 of the Neighborhood layer adds three public tables (``friend_threads``,
``friend_thread_memberships``, ``friend_messages`` via ``friends.0005``). Per
the recurring topo-shift hazard (``0083``/``0097``…``0100``), a new migration
can push a prior relock earlier in the sort, leaving newer tables without RLS —
and ``apps.tenants.test_public_schema_lockdown
.test_rls_enabled_on_owned_public_tables`` fails the build when an owned public
table has ``rowsecurity = false``.

Depends on ``friends.0005`` (creates the tables) and the latest ``tenants``
migration so it runs last. SQL is idempotent and adds NO policy
(``test_no_policies_on_public_schema`` forbids policies on ``public.*``; Django
is BYPASSRLS, so the relock protects the anon Supabase Data API — cross-tenant
isolation is the accessor's job in apps/friends/access.py).
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
    "-- Reversing this migration would leave the friend-chat tables without RLS, "
    "re-exposing them via PostgREST/anon. Do not auto-reverse."
)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0100_relock_after_absorbed_items"),
        ("friends", "0005_friendthread_friendmessage_friendthreadmembership_and_more"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
