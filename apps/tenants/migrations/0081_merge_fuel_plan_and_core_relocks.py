"""Merge the two divergent ``tenants`` leaf migrations.

Concurrent merges left two leaf nodes in the ``tenants`` graph:
``0077_relock_after_fuel_plan_slot`` (fuel-plan reconciler PRs) and
``0080_relock_after_core`` (Core pillar). Django refuses a graph with multiple
leaves, so backend-test/openclaw-doctor on ``main`` fail with
``Conflicting migrations detected; multiple leaf nodes``.

This unifies the graph by depending on both leaves. It also re-runs the same
idempotent public-schema RLS relock the prior migrations use: because each leaf
relocked only the tables visible at *its* point in the topo sort, the union must
be re-locked at the new tip so nothing escapes RLS (the recurring topo-shift
hazard — see ``0066``/``0073``/``0078``/``0080`` and
``feedback_rls_relock_topo_shift``). ``ENABLE ROW LEVEL SECURITY`` on an
already-locked table is a no-op, so this is safe to re-run in prod.

Verified by ``apps.tenants.test_public_schema_lockdown``.
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
    "-- Reversing this migration would leave tables without RLS, re-exposing "
    "them via PostgREST/anon. Do not auto-reverse."
)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0077_relock_after_fuel_plan_slot"),
        ("tenants", "0080_relock_after_core"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
