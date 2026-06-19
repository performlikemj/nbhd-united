"""Re-run the public-schema RLS lockdown after adding the OAuth code table.

The web→app PKCE handoff adds one public table (``oauth_authorization_codes``
via ``tenants.0089``). Per the recurring hazard (see ``0066``, ``0073``,
``0078``, ``0080``, ``0083``, ``0085``), any new migration can push a prior
relock earlier in the topo sort, leaving newer tables without RLS — and
``apps.tenants.test_public_schema_lockdown
.test_rls_enabled_on_owned_public_tables`` fails the build when an owned public
table has ``rowsecurity = false``.

This relock depends on ``0089`` (which creates the table) so it runs after it,
re-locking the new table AND anything else that escaped a previous relock. SQL
is idempotent (ENABLE ROW LEVEL SECURITY on an already-RLS table is a no-op)
and adds NO policy (``test_no_policies_on_public_schema`` forbids policies on
``public.*``). RLS here is defence-in-depth: the table is looked up by opaque
``code_hash`` and the ``/exchange/`` lookup runs as the DB owner (which bypasses
RLS), exactly like the pre-auth ``personal_access_tokens`` lookup.
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
    "-- Reversing this migration would leave oauth_authorization_codes without "
    "RLS, re-exposing it via PostgREST/anon. Do not auto-reverse."
)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0089_oauth_authorization_code"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
