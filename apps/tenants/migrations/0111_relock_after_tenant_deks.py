"""Re-run the public-schema RLS lockdown after adding ``tenant_deks``.

Encryption-at-rest Phase 1 PR2 adds one public table (``tenant_deks`` via
``tenants.0110_tenantdek``). Per the recurring topo-shift hazard
(``0066``/``0073``/``0078``/``0080``/``0083``/``0097``/``0098``/``0099``/``0106``),
a new migration can push a prior relock earlier in the sort, leaving newer
tables without RLS —
``apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables``
fails the build when an owned public table has ``rowsecurity = false``.

Depends on ``tenants.0110_tenantdek`` (creates the table) so it runs last,
re-locking the new table AND anything else that escaped a previous relock.
SQL is idempotent and adds NO policy (``test_no_policies_on_public_schema``
forbids policies on ``public.*``; Django is BYPASSRLS, so this relock only
protects the anon Supabase Data API).

IMPORTANT — this is NOT a contradiction with the boot-time
``apps/tenants/management/commands/disable_rls.py`` sweep, which turns RLS
back OFF for ``tenant_deks`` (and every other non-friends-backstop table) in
every real deployment. ``tenant_deks`` is deliberately NOT added to that
command's ``RLS_KEEP_ENABLED`` set — Phase 1 does not want FORCE-RLS on this
table at runtime, Django remains the tenant boundary here like every other
control-plane table. This migration exists ONLY to satisfy the migration-time
lockdown test, which runs immediately after `migrate` and before
`disable_rls` is invoked in the deploy pipeline. Do not "fix" this by adding
FORCE-RLS policies or by adding ``tenant_deks`` to ``RLS_KEEP_ENABLED`` — see
``apps.tenants.models.TenantDek``'s docstring and
``CONTINUITY_encryption-phase1.md`` §1 PR2.
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
    "-- Reversing this migration would leave tenant_deks without RLS at "
    "migration time, failing the lockdown test. Do not auto-reverse."
)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0110_tenantdek"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
