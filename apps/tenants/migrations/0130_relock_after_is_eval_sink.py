"""Re-run the public-schema RLS lockdown after adding ``Tenant.is_eval_sink``.

Adding a migration can re-sort the global Django migration graph so a prior
relock runs before an unrelated public table is created. Depend on the new field
migration (``0129_tenant_is_eval_sink``) and relock every owned public table at
the new graph tail. This is idempotent and adds no policy; it preserves the anon
Supabase Data API lockdown.

Single-edge shape, matching the ``0119_relock_after_is_synthetic`` precedent for
column-only adds: ``0128_relock_after_typed_crons_default`` already carries the
``cron`` and ``djstripe`` edges, so this relock inherits them transitively and
still lands at the graph tail.

Renumbered: authored as ``0123_tenant_is_eval_sink`` / ``0124_relock_after_is_eval_sink``,
finally landed as ``0129``/``0130`` to stack after PR #1198's typed-crons pair,
which merged to main as ``0127`` / ``0128``.
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
        ("tenants", "0129_tenant_is_eval_sink"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
