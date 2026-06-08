"""Re-run the public-schema RLS lockdown after adding the prepaid-credit table.

The credits feature adds one public money table (``credit_ledger`` via
``billing.0008``). Per the recurring hazard (see ``0066``, ``0073``, ``0078``,
``0080``, ``0083``), any new migration can push a prior relock earlier in the
topo sort, leaving newer tables without RLS — and
``apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables``
fails the build when an owned public table has ``rowsecurity = false``.

This relock depends on ``billing.0008`` (which creates the table) and the
current latest ``tenants`` migration so it runs after them. It ENABLEs RLS on
every owned public table that's missing it (idempotent; no policy added —
``test_no_policies_on_public_schema`` forbids policies on ``public.*``), and
ADDITIONALLY revokes the PostgREST API roles' grants on ``credit_ledger`` — the
real lock for a money table is grant revocation (RLS is the belt). Both the RLS
loop (``tableowner = current_user``) and the REVOKEs (guarded by role existence
+ ``credit_ledger`` being owned by the migration role since it's freshly created)
are no-ops where they don't apply, so this is safe in CI/local Postgres (no
``anon``/``authenticated`` roles) and in prod alike.
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

  -- Money table: explicitly revoke any default grants to the PostgREST API
  -- roles. Guarded so it's a no-op where those roles don't exist (CI/local).
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON public.credit_ledger FROM anon';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    EXECUTE 'REVOKE ALL ON public.credit_ledger FROM authenticated';
  END IF;
END
$$;
"""

REVERSE_SQL = (
    "-- Reversing this migration would leave credit_ledger without RLS / re-grant "
    "the API roles on a money table. Do not auto-reverse."
)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0084_tenant_purchased_credit"),
        ("billing", "0008_creditledger"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
