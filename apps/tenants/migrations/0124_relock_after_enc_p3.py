"""Re-run the public-schema RLS lockdown after the Phase-3 encryption expand.

``tenants.0123_tenant_encrypt_fuel_writes_and_more`` adds the Phase-3 flag pairs
to the ``tenants`` table, and the sibling journal/lessons/insights/core/fuel
migrations add ``*_enc bytea`` sidecar columns to their public tables. Per the
recurring topo-shift hazard
(``0066``/``0073``/``0078``/``0080``/``0083``/``0097``/``0098``/``0099``/``0106``/``0111``/``0114``/``0116``/``0117``/``0119``/``0122``),
adding migrations can re-sort a PRIOR relock earlier in the graph, leaving some
owned public table with ``rowsecurity = false`` at migration time —
``apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables``
fails the build when that happens.

Depends on ``0123`` so it sorts LAST, re-enabling RLS on anything a topo shift
left open. Idempotent, adds NO policy (Django is BYPASSRLS; this only protects
the anon Supabase Data API and satisfies the migration-time lockdown test that
runs before the boot-time ``disable_rls`` sweep). See
``0122_relock_after_sautai_job.py``.
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
        ("tenants", "0123_tenant_encrypt_fuel_writes_and_more"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
