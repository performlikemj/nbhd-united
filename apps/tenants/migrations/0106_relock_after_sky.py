"""Re-run the public-schema RLS lockdown after adding the "My sky" table.

BN-PR1 of the Bounded Neighborhood adds one public table
(``friend_sky_memberships`` via ``friends.0010``). Per the recurring topo-shift
hazard (``0066``/``0073``/``0078``/``0080``/``0083``/``0097``/``0098``/``0099``),
a new migration can push a prior relock earlier in the sort, leaving newer tables
without RLS — and
``apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables``
fails the build when an owned public table has ``rowsecurity = false``.

Depends on ``friends.0010`` (creates the table) and the latest ``tenants``
migration so it runs last, re-locking the new table AND anything else that
escaped a previous relock. SQL is idempotent and adds NO policy
(``test_no_policies_on_public_schema`` forbids policies on ``public.*``; Django
is BYPASSRLS, so the relock protects the anon Supabase Data API — cross-tenant
isolation is the accessor's job in apps/friends/access.py, and "whom you keep
close" stays self-scoped there). The FORCE-RLS + GUC SELECT policy for this
table is the standard defense-in-depth follow-up (brief BN-PR6), not this PR.
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
    "-- Reversing this migration would leave the sky-membership table without "
    "RLS, re-exposing it via PostgREST/anon. Do not auto-reverse."
)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0105_enable_friends_propose_flagged_tenants"),
        ("friends", "0010_skymembership"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
