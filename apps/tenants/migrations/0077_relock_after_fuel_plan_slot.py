"""Re-run the public-schema RLS lockdown after the fuel plan-slot migration.

Adding ``fuel.0012_plan_slot_and_edit_lock`` (Phase 1 of the durable plan
reconciler fix) creates a new ``fuel_plan_slots`` public table. Per
``feedback_rls_relock_topo_shift`` + memory ``project_supabase_public_schema_exposure``,
any new public-table migration can shift Django's topo sort so that the
last relock migration (``tenants.0073_relock_after_typed_crons``) runs
BEFORE the new table is created — leaving it without RLS and breaking
``apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables``.

Same idempotent pattern as 0066 and 0073. Dependencies pin this migration
to run AFTER every app that has a table-creating migration today — when a
future app adds a new public table, add its latest migration here too.

This pass also covers ``byo_models``, which wasn't in 0073's deps list
and may already be un-relocked depending on test topo order. ``agents``
and ``telegram_bot`` have migration files on disk but are NOT in
``INSTALLED_APPS`` — referencing them here would break the migration
loader.

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
    "-- Reversing this migration would leave the plan-slot-era tables "
    "without RLS, re-exposing them via PostgREST/anon. Do not auto-reverse."
)


class Migration(migrations.Migration):
    # Pin this AFTER every app known to create or alter public.* tables.
    # When a new app or migration introduces public tables, add its latest
    # migration here — same convention as 0066 and 0073.
    dependencies = [
        ("tenants", "0076_welcome_email_and_first_message"),
        ("actions", "0001_initial"),
        ("automations", "0001_initial"),
        ("billing", "0006_add_is_system_event"),
        ("byo_models", "0001_initial"),
        ("cron", "0003_typed_cron_patterns"),
        ("djstripe", "0002_2_10"),
        ("finance", "0001_initial"),
        ("fuel", "0012_plan_slot_and_edit_lock"),
        ("insights", "0002_seed_canonical_topics"),
        ("integrations", "0006_unify_google_provider"),
        ("journal", "0019_pendingextraction_goal_pendingextraction_task_and_more"),
        ("lessons", "0002_lesson_position_xy"),
        ("platform_logs", "0001_initial"),
        ("router", "0008_line_quota_state"),
        ("sessions", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
