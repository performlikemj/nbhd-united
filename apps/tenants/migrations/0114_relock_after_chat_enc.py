"""Re-run the public-schema RLS lockdown after the Phase 2 chat ``*_enc`` columns.

Encryption-at-rest Phase 2 PR-1 touches public tables in two apps:
``router.0024_add_chat_enc_columns`` adds ``user_text_enc`` / ``title_enc`` to
``app_chat_messages`` / ``chat_threads``, and ``tenants.0113_add_chat_encryption_flags``
adds the two gate columns to ``tenants``. Per the recurring topo-shift hazard
(``0066``/``0073``/``0078``/``0080``/``0083``/``0097``/``0098``/``0099``/``0106``/``0111``),
adding migrations can re-sort a PRIOR relock earlier in the graph, leaving some
owned public table with ``rowsecurity = false`` at migration time —
``apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables``
fails the build when that happens.

Depends on BOTH new migrations (the router column add AND the tenants flag add)
so it is guaranteed to sort LAST, re-enabling RLS on anything a topo shift left
open. The SQL is idempotent and adds NO policy
(``test_no_policies_on_public_schema`` forbids policies on ``public.*``; Django
is BYPASSRLS, so this relock only protects the anon Supabase Data API).

IMPORTANT — not a contradiction with the boot-time
``apps/tenants/management/commands/disable_rls.py`` sweep, which turns RLS back
OFF at runtime for every table outside its ``RLS_KEEP_ENABLED`` set. Django is
the tenant boundary for the chat tables like every other control-plane table;
this migration exists ONLY to satisfy the migration-time lockdown test, which
runs immediately after ``migrate`` and before ``disable_rls``. Do not "fix" this
by adding FORCE-RLS policies or by adding these tables to ``RLS_KEEP_ENABLED``.
See ``tenants/migrations/0111_relock_after_tenant_deks.py`` and
``CONTINUITY_encryption-phase2.md`` §5 PR-1.
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
    "-- Reversing this migration would leave the Phase 2 chat tables without RLS "
    "at migration time, failing the lockdown test. Do not auto-reverse."
)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0113_add_chat_encryption_flags"),
        ("router", "0024_add_chat_enc_columns"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
