"""Re-run the public-schema RLS lockdown after the eval tables.

``evals.0001_initial`` adds two public tables (``eval_runs`` / ``eval_results``).
Per the recurring topo-shift hazard
(``0066``/``0073``/``0078``/``0080``/``0083``/``0097``/``0098``/``0099``/``0106``/``0111``/``0114``/``0116``),
adding migrations can re-sort a PRIOR relock earlier in the graph, leaving some
owned public table with ``rowsecurity = false`` at migration time —
``apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables``
fails the build when that happens.

Depends on BOTH the evals migration (creates the new tables) AND the latest
tenants migration, so it is guaranteed to sort LAST, re-enabling RLS on anything
a topo shift left open. The SQL is idempotent and adds NO policy
(``test_no_policies_on_public_schema`` forbids policies on ``public.*``; Django
is BYPASSRLS, so this relock only protects the anon Supabase Data API).

IMPORTANT — not a contradiction with the boot-time
``apps/tenants/management/commands/disable_rls.py`` sweep, which turns RLS back
OFF at runtime for every table outside its ``RLS_KEEP_ENABLED`` set. The eval
tables are platform-level (like ``platform_logs``): Django is the boundary, RLS
runs OFF in prod. This migration exists ONLY to satisfy the migration-time
lockdown test, which runs immediately after ``migrate`` and before
``disable_rls``. Do not "fix" this by adding FORCE-RLS policies or by adding the
eval tables to ``RLS_KEEP_ENABLED``. See
``tenants/migrations/0111_relock_after_tenant_deks.py`` and docs/evals-directive.md.
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
    "-- Reversing this migration would leave the eval tables without RLS at "
    "migration time, failing the lockdown test. Do not auto-reverse."
)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0116_relock_after_document_ingestion"),
        ("evals", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
