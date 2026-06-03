"""Re-run the public-schema RLS lockdown after adding the Core pillar.

Creating ``apps.core`` adds two public tables (``core_profiles``,
``core_meditation_sessions``) and shifts Django's migration topo sort. Per the
recurring hazard (see ``0066`` and ``0073``), any new migration can push the
prior relock earlier, leaving newer third-party/app tables without RLS — and
``fuel`` has already drifted from ``0010`` (pinned in 0073) to ``0011`` since.

This relock depends on every public-table app's CURRENT latest migration so it
runs last in the topo sort, catching the new Core tables AND re-locking anything
that escaped 0073. SQL is idempotent (ENABLE ROW LEVEL SECURITY on an
already-RLS table is a no-op). Verified by
``apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables``.
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
    "-- Reversing this migration would leave the Core-era tables without RLS, "
    "re-exposing them via PostgREST/anon. Do not auto-reverse."
)


class Migration(migrations.Migration):
    # Pin AFTER every app that creates or alters public.* tables (current latest
    # migration each). When a new app/table lands, add a fresh relock like this.
    dependencies = [
        ("tenants", "0078_tenant_core_enabled"),
        ("core", "0001_initial"),
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
        ("lessons", "0002_lesson_position_xy"),
        ("automations", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
