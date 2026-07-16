"""Re-run the public-schema RLS lockdown after this PR's two migrations.

Adding ``tenants.0127_alter_tenant_experimental_typed_crons`` +
``cron.0005_remove_cronjob_cron_unique_tenant_name_and_more`` re-sorted Django's
migration topo graph, so the most recent relock
(``0122_relock_after_sautai_job``) ran BEFORE ``djstripe.0003_2_11`` created
``djstripe_accountv2`` / ``djstripe_productfeature`` — those two tables then
escaped the lockdown with ``rowsecurity = false`` and
``apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables``
failed the build. Same hazard, same shape, and the same fix as
``0073_relock_after_typed_crons`` (which was itself triggered by the ORIGINAL
typed-cron migrations) and the eighteen relocks before it.

It reproduced ONLY in CI, which is worth recording: dj-stripe is pinned to
2.11.0 (requirements.txt) and 2.11.0 is the version that adds those two tables.
A local venv still on 2.10.x has no ``djstripe.0003_2_11`` at all, so the test
passes locally and fails in CI — do not trust a local green here.

``("djstripe", "__latest__")`` is deliberate rather than pinning ``0003_2_11``.
Django resolves it to whatever leaf the installed dj-stripe actually has
(``MigrationLoader.check_key``), so this keeps working across a stale local venv
AND the next dependabot bump — and a dependabot bump introducing new tables is
precisely the mechanism that broke it this time. A hard pin would break the
migration graph on any environment that has not upgraded yet.

Idempotent (``ENABLE ROW LEVEL SECURITY`` on an already-locked table is a no-op)
and adds NO policy — Django connects as BYPASSRLS; this only protects the anon
Supabase Data API and satisfies the migration-time lockdown test that runs before
the boot-time ``disable_rls`` sweep. See ``0117_relock_after_eval_tables.py``.

This pair was renumbered 0123/0124 → 0125/0126 because encryption Phase 3 landed
its own ``0123_tenant_encrypt_fuel_writes_and_more`` /
``0124_relock_after_enc_p3`` on main first — two leaf nodes in ``tenants`` would
otherwise have wedged ``migrate --noinput`` at container boot.
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
    # Pinned AFTER both of this PR's migrations (the topo shift) and after every
    # djstripe migration (the tables that escaped). Guarantees this sorts last.
    dependencies = [
        ("tenants", "0127_alter_tenant_experimental_typed_crons"),
        ("cron", "0005_remove_cronjob_cron_unique_tenant_name_and_more"),
        ("djstripe", "__latest__"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
