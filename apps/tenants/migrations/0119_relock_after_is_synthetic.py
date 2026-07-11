"""Re-run the public-schema RLS lockdown after adding ``Tenant.is_synthetic``.

``tenants.0118_tenant_is_synthetic`` adds a column to the ``tenants`` table.
Per the recurring topo-shift hazard
(``0066``/``0073``/``0078``/``0080``/``0083``/``0097``/``0098``/``0099``/``0106``/``0111``/``0114``/``0116``/``0117``),
adding migrations can re-sort a PRIOR relock earlier in the graph, leaving some
owned public table with ``rowsecurity = false`` at migration time —
``apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables``
fails the build when that happens.

Depends on ``0118`` so it sorts LAST, re-enabling RLS on anything a topo shift
left open. Idempotent, adds NO policy (Django is BYPASSRLS; this only protects
the anon Supabase Data API and satisfies the migration-time lockdown test that
runs before the boot-time ``disable_rls`` sweep). See
``0111_relock_after_tenant_deks.py``.
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

REVERSE_SQL = "-- Do not auto-reverse; would leave tables unlocked at migration time (lockdown test)."


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0118_tenant_is_synthetic"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
