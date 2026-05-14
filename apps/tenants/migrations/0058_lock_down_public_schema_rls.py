"""Lock down the public schema against the Supabase Data API.

Django connects as `postgres` (BYPASSRLS) and bypasses RLS, so enabling RLS
on every public table has no effect on the app — but it closes the
PostgREST-with-anon-key exposure surface as defense in depth. The matching
`disable_rls` management command and its `startup.sh` invocation are removed
in the same change.

Three things happen:

1. Drop every policy on public.* tables. The pre-existing
   `service_bypass` / `tenant_isolation_*` / `user_isolation_*` policies
   were written for a Supabase-Auth model that was never wired up; they
   target the `public` role (which includes `anon`) so leaving them in
   place would defeat the lockdown the moment RLS is turned on.

2. Enable RLS on every public.* base table the migrating role owns. With
   no permissive policies, only owners and BYPASSRLS roles (`postgres`,
   `service_role`, `supabase_admin`) can read or write.

3. Revoke all privileges on public.* from the Supabase API roles `anon`
   and `authenticated`, and adjust default privileges so future tables
   inherit the same posture. Guarded by role-existence checks so the
   migration is a no-op on local dev where those roles don't exist.
"""

from django.db import migrations

LOCK_DOWN_SQL = r"""
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT n.nspname AS schemaname, c.relname AS tablename, p.polname AS policyname
    FROM pg_policy p
    JOIN pg_class c ON c.oid = p.polrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND pg_get_userbyid(c.relowner) = current_user
  LOOP
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I.%I',
                   r.policyname, r.schemaname, r.tablename);
  END LOOP;

  FOR r IN
    SELECT schemaname, tablename
    FROM pg_tables
    WHERE schemaname = 'public'
      AND tableowner = current_user
  LOOP
    EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
                   r.schemaname, r.tablename);
  END LOOP;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM anon';
    EXECUTE 'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM anon';
    EXECUTE 'REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM anon';
    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon';
    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon';
    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM anon';
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    EXECUTE 'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM authenticated';
    EXECUTE 'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM authenticated';
    EXECUTE 'REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM authenticated';
    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM authenticated';
    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM authenticated';
    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM authenticated';
  END IF;
END
$$;
"""


REVERSE_SQL = (
    "-- Reversing this migration would re-expose the public schema "
    "to the Supabase Data API via anon/authenticated. If you genuinely "
    "need that, do it deliberately in a follow-up migration with explicit "
    "policies, not by reversing."
)


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0057_tenant_internal_api_key"),
    ]

    operations = [
        migrations.RunSQL(LOCK_DOWN_SQL, REVERSE_SQL),
    ]
