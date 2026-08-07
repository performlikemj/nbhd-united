"""Unit and command coverage for retiring denylisted entity bindings."""

from __future__ import annotations

import secrets
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from apps.pii.entity_registry import retire_bindings_for_key
from apps.tenants.models import Tenant, User


class RetireBindingsForKeyTests(SimpleTestCase):
    def test_returns_new_map_and_only_newly_retired_canonical_matches(self):
        original = {
            "[PERSON_1]": " NBHD ",
            "[PERSON_2]": {"name": "nbhd", "relationship": "team"},
            "[PERSON_3]": {"name": "NBHD", "retired": True, "retired_at": "earlier"},
            "[PERSON_4]": "Other",
        }

        updated, placeholders = retire_bindings_for_key(
            original,
            "nbhd",
            now_iso="2026-08-07T01:02:03+00:00",
        )

        self.assertIsNot(updated, original)
        self.assertEqual(placeholders, ["[PERSON_1]", "[PERSON_2]"])
        self.assertEqual(original["[PERSON_1]"], " NBHD ")
        self.assertNotIn("retired", original["[PERSON_2]"])
        self.assertEqual(
            updated["[PERSON_1]"],
            {
                "name": " NBHD ",
                "retired": True,
                "retired_at": "2026-08-07T01:02:03+00:00",
            },
        )
        self.assertEqual(updated["[PERSON_2]"]["relationship"], "team")
        self.assertTrue(updated["[PERSON_2]"]["retired"])
        self.assertEqual(updated["[PERSON_3]"]["retired_at"], "earlier")
        self.assertEqual(updated["[PERSON_4]"], "Other")


def _make_tenant(*, entity_map: dict, denylist: dict) -> Tenant:
    user = User.objects.create_user(
        username=f"u_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        password="hunter2-test",
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="container.example.com",
        pii_entity_map=entity_map,
        pii_denylist=denylist,
    )


class RetireDeniedBindingsCommandTests(TestCase):
    def test_dry_run_reports_grouped_counts_and_writes_nothing(self):
        tenant = _make_tenant(
            entity_map={
                "[PERSON_1]": "NBHD",
                "[PERSON_2]": {"name": " nbhd "},
                "[PERSON_3]": {"name": "Alice"},
                "[PERSON_4]": {"name": "Other"},
            },
            denylist={"nbhd": {}, "alice": {}},
        )
        before = tenant.pii_entity_map
        stdout = StringIO()

        call_command("retire_denied_bindings", str(tenant.id), stdout=stdout)

        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_entity_map, before)
        output = stdout.getvalue()
        self.assertIn(f"tenant={tenant.id} would_retire=3", output)
        self.assertIn("by_key=alice=1,nbhd=2", output)
        self.assertIn("[DRY-RUN] tenants_scanned=1 tenants_with_matches=1 bindings_would_retire=3", output)

    def test_commit_retires_all_denied_bindings_and_is_idempotent(self):
        first = _make_tenant(
            entity_map={
                "[PERSON_1]": "NBHD",
                "[PERSON_2]": {"name": "nbhd"},
                "[PERSON_3]": {"name": "Alice"},
                "[PERSON_4]": {"name": "Other"},
                "[PERSON_5]": {
                    "name": "Alice",
                    "retired": True,
                    "retired_at": "2026-01-01T00:00:00+00:00",
                },
            },
            denylist={"nbhd": {}, "alice": {}},
        )
        second = _make_tenant(
            entity_map={"[PERSON_8]": {"name": "Bob"}},
            denylist={"bob": {}},
        )
        stdout = StringIO()

        call_command("retire_denied_bindings", "--all", "--commit", stdout=stdout)

        first.refresh_from_db()
        second.refresh_from_db()
        for placeholder in ("[PERSON_1]", "[PERSON_2]", "[PERSON_3]", "[PERSON_5]"):
            self.assertTrue(first.pii_entity_map[placeholder]["retired"])
        self.assertNotIn("retired", first.pii_entity_map["[PERSON_4]"])
        self.assertTrue(second.pii_entity_map["[PERSON_8]"]["retired"])
        self.assertIn("bindings_retired=4", stdout.getvalue())

        second_stdout = StringIO()
        call_command("retire_denied_bindings", "--all", "--commit", stdout=second_stdout)

        self.assertIn("bindings_retired=0", second_stdout.getvalue())
        self.assertIn("tenants_with_matches=0", second_stdout.getvalue())
