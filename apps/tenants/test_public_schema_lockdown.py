"""CI guard against re-exposing the public schema to the Supabase Data API.

Background: migration 0058_lock_down_public_schema_rls and
memory/project_supabase_public_schema_exposure.md document the 2026-05-14
finding that the anon API key could read `users.email` + Django password
hashes via PostgREST. The hole opened because Supabase grants
`anon`/`authenticated` full DML on every public table by default and the
pre-existing `tenant_isolation_*` / `service_bypass` policies targeted the
`public` pseudo-role (which includes `anon`).

This test fails the build if either pattern reappears.

If a future change genuinely needs one of these patterns, add the comment
`rls-exposure-allowed` on the same migration file and update this test's
allowlist deliberately.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.db import connection
from django.test import SimpleTestCase, TestCase

MIGRATIONS_GLOB = "apps/*/migrations/0*.py"

POLICY_TO_PUBLIC = re.compile(
    r"CREATE\s+POLICY[^;]*?\bTO\s+public\b",
    re.IGNORECASE,
)

GRANT_TO_API_ROLE = re.compile(
    r"\bGRANT\b[^;]*?\bTO\s+(?:anon|authenticated)\b",
    re.IGNORECASE,
)

DISABLE_RLS_STMT = re.compile(
    r"\bDISABLE\s+ROW\s+LEVEL\s+SECURITY\b",
    re.IGNORECASE,
)

EXEMPTION = "rls-exposure-allowed"


def _migration_files() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[2]
    return sorted(repo_root.glob(MIGRATIONS_GLOB))


class PublicSchemaLockdownStaticGuard(SimpleTestCase):
    """Static analysis of migration files — runs without a DB."""

    def test_no_policies_target_public_role(self):
        offenders: list[tuple[str, str]] = []
        for path in _migration_files():
            text = path.read_text()
            if EXEMPTION in text:
                continue
            for m in POLICY_TO_PUBLIC.finditer(text):
                offenders.append((str(path), text[m.start() : m.end()][:120]))
        self.assertEqual(
            offenders,
            [],
            "Found `CREATE POLICY ... TO public` in migration(s). The Postgres "
            "`public` pseudo-role includes `anon`, so this re-exposes the table "
            f"via the Supabase Data API. Name a specific role or add a "
            f"`{EXEMPTION}` comment in the file if truly intentional. "
            f"Offenders: {offenders}",
        )

    def test_no_grants_to_supabase_api_roles(self):
        offenders: list[tuple[str, str]] = []
        for path in _migration_files():
            text = path.read_text()
            if EXEMPTION in text:
                continue
            for m in GRANT_TO_API_ROLE.finditer(text):
                offenders.append((str(path), text[m.start() : m.end()][:120]))
        self.assertEqual(
            offenders,
            [],
            "Found `GRANT ... TO anon|authenticated` in migration(s). These "
            "re-expose tables via the Supabase Data API. Add a "
            f"`{EXEMPTION}` comment if intentional. Offenders: {offenders}",
        )

    def test_no_new_disable_rls_in_migrations(self):
        offenders: list[tuple[str, str]] = []
        for path in _migration_files():
            text = path.read_text()
            if EXEMPTION in text:
                continue
            for m in DISABLE_RLS_STMT.finditer(text):
                offenders.append((str(path), text[m.start() : m.end()][:120]))
        self.assertEqual(
            offenders,
            [],
            "Found `DISABLE ROW LEVEL SECURITY` in migration(s). Toggling "
            "RLS off at the migration layer defeats the lockdown installed "
            f"by 0058_lock_down_public_schema_rls. Add a `{EXEMPTION}` "
            f"comment if intentional. Offenders: {offenders}",
        )


class PublicSchemaLockdownRuntimeGuard(TestCase):
    """Asserts the post-migrate state of the test database."""

    def test_no_policies_on_public_schema(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT tablename, policyname, roles
                FROM pg_policies
                WHERE schemaname = 'public'
                ORDER BY tablename, policyname
                """
            )
            rows = cur.fetchall()
        self.assertEqual(
            rows,
            [],
            "Policies exist on public.* after migrations run. Migration 0058 "
            "drops every existing policy; if you need a real RLS policy add "
            "it via a new migration with a named role (never `TO public`) "
            "and update this test. Found: " + repr(rows),
        )

    def test_rls_enabled_on_owned_public_tables(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                  AND tableowner = current_user
                  AND rowsecurity = false
                ORDER BY tablename
                """
            )
            offenders = [row[0] for row in cur.fetchall()]
        self.assertEqual(
            offenders,
            [],
            "Tables in public.* (owned by the test role) have RLS disabled "
            "after migrations run. Migration 0058 enables RLS on all owned "
            "public tables; if you genuinely need RLS off for a new table, "
            "document why and update this test. Offenders: "
            + repr(offenders),
        )
