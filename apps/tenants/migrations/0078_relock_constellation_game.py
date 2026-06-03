"""Re-run the public-schema RLS lockdown after the constellation-game tables.

PR #754 added ``lessons.0003_add_star_lifecycle_and_tutoring``, which creates
the ``tutoring_sessions`` and ``star_journal_entries`` public tables. Because
that migration lands AFTER ``tenants.0073_relock_after_typed_crons`` in Django's
migration topo sort, the new tables were created once the previous relock sweep
had already run — so they escaped RLS and re-exposed themselves via
PostgREST/anon, breaking
``apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables``
(offenders: ``star_journal_entries``, ``tutoring_sessions``).

Same documented topo-shift trap fixed by ``0066_relock_public_schema_rls`` and
``0073_relock_after_typed_crons``. Dependencies pin this migration to run AFTER
every app that creates public.* tables — and after the constellation-game
additions — so the lockdown catches everything regardless of which app's
migration the topo sort happens to interleave last.

The relock SQL is idempotent — running ``ALTER TABLE ... ENABLE ROW LEVEL
SECURITY`` on a table that already has RLS enabled is a no-op.
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
    "-- Reversing this migration would leave the constellation-game tables "
    "without RLS, re-exposing them via PostgREST/anon. Do not auto-reverse."
)


class Migration(migrations.Migration):
    # Pin this AFTER every app known to create or alter public.* tables.
    # When a new app or migration introduces public tables, add its latest
    # migration here — same convention as 0066 / 0073.
    dependencies = [
        ("tenants", "0077_alter_tenant_openclaw_version"),
        ("cron", "0003_typed_cron_patterns"),
        ("journal", "0019_pendingextraction_goal_pendingextraction_task_and_more"),
        ("router", "0008_line_quota_state"),
        ("platform_logs", "0001_initial"),
        ("sessions", "0001_initial"),
        ("actions", "0001_initial"),
        ("billing", "0006_add_is_system_event"),
        ("djstripe", "0002_2_10"),
        ("finance", "0001_initial"),
        ("fuel", "0011_fuelprofile_distance_unit"),
        ("insights", "0002_seed_canonical_topics"),
        ("integrations", "0006_unify_google_provider"),
        ("lessons", "0003_add_star_lifecycle_and_tutoring"),
        ("automations", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
