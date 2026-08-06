"""Disable Row-Level Security on all tables.

Supabase re-enables RLS on tables created by migrations.
Run this after every `migrate` to ensure the application can
read/write without per-row policies blocking access.

EXCEPTION (friends DB backstop + transcript ledger): the four friends tables
and three ciphertext-only transcript tables carry deliberate FORCE-RLS tenant
policies. Those MUST survive this boot-time sweep, or the backstop would be
wiped on every deploy. Everything else keeps the pre-existing fleet posture
(RLS off; the anon Data API has zero grants on these tables, so isolation is
the Django layer). Keep this list in sync with the policy migrations and
apps.tenants.test_public_schema_lockdown's allowlist.
"""

from django.core.management.base import BaseCommand
from django.db import connection

# Tables whose FORCE-RLS policies must NOT be disabled at boot.
RLS_KEEP_ENABLED = frozenset(
    {
        "shared_lessons",
        "lesson_share_grants",
        "friend_messages",
        "friend_sky_memberships",
        "transcripts_transcriptevent",
        "transcripts_transcriptcapturequarantine",
        "transcripts_transcriptindexoutbox",
    }
)


class Command(BaseCommand):
    help = "Disable RLS on user tables except explicit FORCE-RLS backstops."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            # Only select tables owned by the current database user,
            # skipping Supabase internal tables (e.g. saml_relay_states).
            cursor.execute(
                """
                SELECT schemaname, tablename
                FROM pg_tables
                WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
                  AND rowsecurity = true
                  AND tableowner = current_user
                """
            )
            tables = cursor.fetchall()

            disabled = 0
            for schema, table in tables:
                if table in RLS_KEEP_ENABLED:
                    self.stdout.write(f"  Kept RLS ENABLED on {schema}.{table} (explicit backstop)")
                    continue
                fqn = f'"{schema}"."{table}"'
                cursor.execute(f"ALTER TABLE {fqn} DISABLE ROW LEVEL SECURITY;")
                self.stdout.write(f"  Disabled RLS on {fqn}")
                disabled += 1

            if disabled == 0:
                self.stdout.write("No tables to disable (owned by current user, not exempt).")
                return
            self.stdout.write(self.style.SUCCESS(f"Disabled RLS on {disabled} table(s)."))
