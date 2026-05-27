"""Re-run the public-schema RLS lockdown after the typed-cron migration.

Adding ``cron.0003_typed_cron_patterns`` + ``tenants.0072_typed_cron_patterns``
shifted Django's migration topo sort so that ``tenants.0066_relock_public_schema_rls``
ran BEFORE the djstripe / finance / fuel / integrations / cron migrations
created their tables — those tables then escaped the lockdown, breaking
``apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables``
(verified locally + on PR #715 CI).

Same pattern as ``0066_relock_public_schema_rls`` (which itself was a
relock-after-reorder fix). Dependencies pin this migration to run AFTER
every app that creates public.* tables — and after the typed-cron
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
    "-- Reversing this migration would leave the typed-cron-era tables "
    "without RLS, re-exposing them via PostgREST/anon. Do not auto-reverse."
)


class Migration(migrations.Migration):
    # Pin this AFTER every app known to create or alter public.* tables.
    # When a new app or migration introduces public tables, add its latest
    # migration here — same convention as 0066.
    dependencies = [
        ("tenants", "0072_typed_cron_patterns"),
        ("cron", "0003_typed_cron_patterns"),
        ("journal", "0019_pendingextraction_goal_pendingextraction_task_and_more"),
        ("router", "0008_line_quota_state"),
        ("platform_logs", "0001_initial"),
        ("sessions", "0001_initial"),
        ("actions", "0001_initial"),
        ("billing", "0006_add_is_system_event"),
        ("djstripe", "0002_2_10"),
        ("finance", "0001_initial"),
        ("fuel", "0010_stamp_set_type"),
        ("insights", "0002_seed_canonical_topics"),
        ("integrations", "0006_unify_google_provider"),
        ("lessons", "0002_lesson_position_xy"),
        ("automations", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(RELOCK_SQL, REVERSE_SQL),
    ]
