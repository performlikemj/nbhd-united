"""Stop Postgres from killing idle pooled ``app_user`` backends.

The production DB role ``app_user`` carried ``idle_session_timeout = 60s`` — a
leftover from the 2026-05-15 session-mode → transaction-mode pooler migration
(see ``config/settings/production.py``). Under the **transaction-mode** Supavisor
pooler, backend sessions are returned to the pool between transactions and sit
idle there legitimately. On the low-traffic control plane they routinely sit
idle > 60s, so Postgres reaps them; Supavisor then hands a reaped backend to the
next request without revalidating, and the first statement (RLS ``set_config`` /
user load) throws::

    psycopg.errors.IdleSessionTimeout: terminating connection due to idle-session timeout
    -> django.db.utils.OperationalError  -> HTTP 500

This surfaced as intermittent "Couldn't load (HTTP 500)" on the iOS app's
``/api/v1/...`` GETs (e.g. ``fuel/workouts/<id>/``) and on cron endpoints —
the classic "first request after the pool went idle" pattern.

Under transaction pooling a Django-side health-check/retry is unreliable (each
transaction may get a different backend, so you can't validate the one the next
query will use). The reliable fix is to stop reaping pooled backends and let the
pooler own their lifecycle:

  * ``idle_session_timeout = 0``   — disable the reaper (the pooler manages this).
  * ``idle_in_transaction_session_timeout = '30s'`` — ADD the guard that actually
    matters: reap sessions that abandon an *open transaction* (holding locks),
    which the role previously lacked. Net robustness gain; neither setting touches
    tenant isolation (that's RLS policies + grant revocation + app-level query
    filtering, not session reaping).

Already applied to prod manually via the Supabase connection; this migration
codifies it so the role config can't silently drift again. Guarded so it's a
no-op where ``app_user`` doesn't exist (CI/local Postgres) or where the migrate
role lacks privilege to alter it.
"""

from django.db import migrations

FORWARD_SQL = r"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    BEGIN
      EXECUTE 'ALTER ROLE app_user SET idle_session_timeout = 0';
      EXECUTE 'ALTER ROLE app_user SET idle_in_transaction_session_timeout = ''30s''';
    EXCEPTION WHEN insufficient_privilege THEN
      RAISE NOTICE 'tenants.0087: insufficient privilege to ALTER ROLE app_user; '
                   'apply manually (already done in prod).';
    END;
  END IF;
END
$$;
"""

REVERSE_SQL = r"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    BEGIN
      EXECUTE 'ALTER ROLE app_user RESET idle_session_timeout';
      EXECUTE 'ALTER ROLE app_user RESET idle_in_transaction_session_timeout';
    EXCEPTION WHEN insufficient_privilege THEN
      RAISE NOTICE 'tenants.0087 reverse: insufficient privilege; skipping.';
    END;
  END IF;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0086_migrate_minimax_preferred_model"),
    ]

    operations = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
