"""Re-run the public-schema RLS lockdown after adding the document-ingestion tables.

Document information-keeping adds two owned public tables (``journal_document_ingestions``
+ ``journal_document_ingestion_artifacts`` via ``journal.0022``). Per the recurring
hazard (see ``0066``, ``0073``, ``0078``, ``0080``, ``0083``, ``0085``, ``0090``), any
new migration can push a prior relock earlier in the topo sort, leaving newer tables
without RLS — and ``apps.tenants.test_public_schema_lockdown
.test_rls_enabled_on_owned_public_tables`` fails the build when an owned public table
has ``rowsecurity = false``.

This relock depends on ``journal.0022`` (which creates + RLS-enables the tables) so it
runs after them, re-locking anything else that escaped a previous relock. SQL is
idempotent (ENABLE ROW LEVEL SECURITY on an already-RLS table is a no-op) and adds NO
policy (``test_no_policies_on_public_schema`` forbids policies on ``public.*``). RLS
here is defence-in-depth: both tables are looked up only under a validated tenant scope
by the runtime/console forget paths, which run as the DB owner (which bypasses RLS).
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
    "-- Reversing this migration would leave the document-ingestion tables without "
    "RLS, re-exposing them via PostgREST/anon. Do not auto-reverse."
)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0110_tenant_document_ingestion_enabled"),
        ("journal", "0022_document_ingestion"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
