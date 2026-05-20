"""Re-run the public-schema RLS lockdown after newer app migrations.

Background: ``0059_lock_down_public_schema_rls`` enables RLS by iterating
``pg_tables`` at migration runtime — it catches only tables that *exist
at the moment it runs*. Migrations in other apps that don't share a
dependency edge with ``tenants`` can be scheduled by Django's topo sort
either before or after 0059. The ``token_blacklist`` fix in 0059
(cd94f74) handles one third-party case, but new first-party migrations
that introduce a fresh ``tenants → journal`` dependency chain (e.g.
``journal/0018`` → ``tenants/0065``) reorder the plan and push
``journal/*``, ``router/*``, and ``platform_logs/*`` migrations to run
*after* 0059. Their tables then escape the lockdown.

This migration replays the lockdown loop, scheduled after every app
known to create ``public.*`` base tables. New apps that add public
tables should add their latest migration to ``dependencies`` here (or
to ``0059`` if they pre-date this migration's existence).
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
    "-- Reversing this migration would leave new public.* tables without "
    "RLS, re-exposing them via PostgREST/anon. Do not auto-reverse."
)


class Migration(migrations.Migration):
    # Depend on the latest migration of every app that creates public.*
    # tables. Forces them to run BEFORE this lockdown replay so their
    # tables are caught.
    dependencies = [
        ("tenants", "0065_goal_task_typed_lifecycle"),
        ("journal", "0018_goal_task_typed_lifecycle"),
        ("router", "0005_processedinboundevent"),
        ("platform_logs", "0001_initial"),
        ("sessions", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
