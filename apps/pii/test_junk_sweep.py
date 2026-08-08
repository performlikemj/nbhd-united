"""Tests for the zero-egress deterministic PII junk sweep (apps/pii/junk_sweep.py).

These pin the behavior the prod audit demands: the sweep must cull the machine-
text junk classes (agent markdown, invisible-char runs, placeholder fragments,
date/number mislabels, unvalidated financial labels) while leaving real-looking
PERSON/LOCATION/email/card bindings alone. It must heal owner-visible journal
text BEFORE dropping a binding (so no raw placeholder is stranded), and it must
NEVER rewrite raw value text — only exact ``[TYPE_N]`` / ``\\[TYPE_N\\]`` tokens.

All fixtures use throwaway values (example.com, "Sarah Chen") — no real PII.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from django.test import TestCase

from apps.journal.models import Document, DocumentChunk, Goal, JournalEntry, PendingTaskAction, Task
from apps.pii import junk_sweep
from apps.pii.junk_sweep import classify_entry, sweep_all_tenants, sweep_tenant
from apps.pii.store_registry import PlaceholderStore
from apps.tenants.models import Tenant, User

# 8 junk bindings spanning the audit's deterministic-junk classes, keyed to the
# reason code classify_entry should emit.
JUNK_MAP = {
    "[PERSON_100]": {"name": "Quick Wins\n-"},  # structure (markdown)
    "[PERSON_101]": {"name": "|----|----|"},  # structure (table divider)
    "[LOCATION_102]": {"name": "### 08:05 — Neighbor"},  # structure (heading)
    "[PERSON_103]": {"name": "​Newsletter Sender"},  # invisible (zero-width)
    "[CRYPTO_ADDRESS_104]": {"name": "[CRYP"},  # placeholder_fragment
    "[ACCOUNT_105]": {"name": "2026-05-30"},  # numeric_datelike
    "[CREDIT_CARD_106]": {"name": "django"},  # invalid_credit_card
    "[ACCOUNT_107]": {"name": "18–29°C"},  # invalid_account (temp range)
}

# 4 real-looking keepers.
KEEP_MAP = {
    "[PERSON_1]": {"name": "Sarah Chen"},
    "[LOCATION_1]": {"name": "Shibuya"},
    "[EMAIL_ADDRESS_1]": {"name": "sarah@example.com"},
    "[CREDIT_CARD_1]": {"name": "4111111111111111"},  # Luhn-valid Visa test PAN
}

# Non-denyable junk keys: no letter, so a denylist entry would be uselessly
# broad. They are deleted but never denied.
_NON_DENYABLE_KEYS = {"|----|----|", "2026-05-30"}


class ClassifyEntryTests(TestCase):
    def test_junk_classes_flagged_with_reason(self):
        expected = {
            "[PERSON_100]": "structure",
            "[PERSON_101]": "structure",
            "[LOCATION_102]": "structure",
            "[PERSON_103]": "invisible",
            "[CRYPTO_ADDRESS_104]": "placeholder_fragment",
            "[ACCOUNT_105]": "numeric_datelike",
            "[CREDIT_CARD_106]": "invalid_credit_card",
            "[ACCOUNT_107]": "invalid_account",
        }
        for placeholder, reason in expected.items():
            verdict, got = classify_entry(placeholder, JUNK_MAP[placeholder]["name"])
            self.assertEqual(verdict, "junk", placeholder)
            self.assertEqual(got, reason, placeholder)

    def test_keepers_kept(self):
        for placeholder, entry in KEEP_MAP.items():
            verdict, _ = classify_entry(placeholder, entry["name"])
            self.assertEqual(verdict, "keep", placeholder)

    def test_person_common_word_is_not_structurally_junk(self):
        # A plain lowercase word tagged PERSON is NOT deterministic junk — the
        # cloud arbiter judged those; the on-device review flow does now. The
        # sweep must not cull it (bias: false-junk on real PII is the failure).
        verdict, _ = classify_entry("[PERSON_9]", "goal")
        self.assertEqual(verdict, "keep")

    def test_malformed_placeholder_falls_back_to_hygiene_only(self):
        # Unknown/empty type → no structured validation, only is_junk_span.
        self.assertEqual(classify_entry("not-a-placeholder", "Sarah")[0], "keep")
        self.assertEqual(classify_entry("not-a-placeholder", "a|b")[0], "junk")


class _TenantMixin:
    def _make_tenant(self, *, username: str, entity_map=None, denylist=None, status="active") -> Tenant:
        user = User.objects.create_user(username=username, password="x")
        tenant = Tenant.objects.create(
            user=user,
            status=status,
            pii_entity_map=dict(entity_map or {}),
            pii_denylist=dict(denylist or {}),
        )
        return tenant


class SweepTenantTests(_TenantMixin, TestCase):
    def setUp(self):
        self.tenant = self._make_tenant(
            username="owner",
            entity_map={**JUNK_MAP, **KEEP_MAP},
        )
        # Document with BOTH plain and markdown-escaped junk tokens plus a
        # keeper token that must survive untouched.
        self.doc = Document.objects.create(
            tenant=self.tenant,
            kind="project",
            slug="healme",
            title="Trip",
            markdown="Log [ACCOUNT_105] and \\[CREDIT_CARD_106\\] with [PERSON_1] noted.",
        )
        # Raw value text with NO bracket — the risk fixture: "2026-05-30" and
        # "django" appear as prose and MUST NOT be rewritten (only tokens are).
        self.raw_doc = Document.objects.create(
            tenant=self.tenant,
            kind="project",
            slug="rawname",
            title="Entry",
            markdown="Deployed django on 2026-05-30 with no token.",
        )
        self.task = Task.objects.create(
            tenant=self.tenant,
            title="Pay [ACCOUNT_105]",
            pii_receipts={
                "title": {
                    "state": "placeholder",
                    "redactions": [{"placeholder": "[ACCOUNT_105]", "value": "2026-05-30"}],
                }
            },
        )
        self.goal = Goal.objects.create(
            tenant=self.tenant,
            title="Goal",
            description="Ref \\[CREDIT_CARD_106\\] here",
        )
        self.chunk = DocumentChunk.objects.create(
            tenant=self.tenant,
            document=self.doc,
            chunk_index=0,
            text="chunk [CREDIT_CARD_106] x",
            embedding=[0.0] * 1536,
        )
        self.pending_action = PendingTaskAction.objects.create(
            tenant=self.tenant,
            kind=PendingTaskAction.Kind.TASK_PROGRESS,
            evidence="Evidence [CREDIT_CARD_106]",
            pii_receipts={
                "evidence": {
                    "state": "placeholder",
                    "redactions": [{"placeholder": "[CREDIT_CARD_106]", "value": "django"}],
                }
            },
            source_date="2026-08-07",
        )

    def test_full_sweep_heals_denies_deletes(self):
        result = sweep_tenant(self.tenant)

        # Counts.
        self.assertEqual(result["examined"], 12)
        self.assertEqual(result["junk"], 8)
        self.assertEqual(result["skipped"], 4)
        self.assertEqual(result["deleted"], 8)
        self.assertEqual(result["denied"], 6)  # 8 junk − 2 non-denyable
        self.assertEqual(result["healed_rows"], 5)  # doc, task, goal, chunk, reconciliation evidence

        self.tenant.refresh_from_db()

        # (a) markdown healed — BOTH plain and escaped forms → bound value.
        self.doc.refresh_from_db()
        self.assertEqual(
            self.doc.markdown,
            "Log 2026-05-30 and django with [PERSON_1] noted.",
        )
        # Keeper token untouched.
        self.assertIn("[PERSON_1]", self.doc.markdown)

        # RISK: raw value prose is never rewritten (no token present).
        self.raw_doc.refresh_from_db()
        self.assertEqual(self.raw_doc.markdown, "Deployed django on 2026-05-30 with no token.")

        # Task / Goal / DocumentChunk healed.
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Pay 2026-05-30")
        self.assertEqual(self.task.pii_receipts["title"]["redactions"], [])
        self.goal.refresh_from_db()
        self.assertEqual(self.goal.description, "Ref django here")
        self.chunk.refresh_from_db()
        self.assertEqual(self.chunk.text, "chunk django x")
        self.pending_action.refresh_from_db()
        self.assertEqual(self.pending_action.evidence, "Evidence django")
        self.assertEqual(self.pending_action.pii_receipts["evidence"]["redactions"], [])

        # (b) denylist gained canonical keys with the junk-sweep reason.
        self.assertIn("django", self.tenant.pii_denylist)
        self.assertTrue(self.tenant.pii_denylist["django"]["reason"].startswith("junk-sweep:invalid_credit_card"))
        self.assertIn("decided_at", self.tenant.pii_denylist["django"])
        for key in _NON_DENYABLE_KEYS:
            self.assertNotIn(key, self.tenant.pii_denylist)

        # (c) junk bindings gone; keepers intact.
        for placeholder in JUNK_MAP:
            self.assertNotIn(placeholder, self.tenant.pii_entity_map)
        for placeholder in KEEP_MAP:
            self.assertIn(placeholder, self.tenant.pii_entity_map)

    def test_dry_run_mutates_nothing(self):
        before_map = dict(self.tenant.pii_entity_map)
        before_deny = dict(self.tenant.pii_denylist)

        result = sweep_tenant(self.tenant, dry_run=True)

        self.assertEqual(result["junk"], 8)
        self.assertEqual(result["healed_rows"], 0)
        self.assertEqual(result["denied"], 0)
        self.assertEqual(result["deleted"], 0)

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map, before_map)
        self.assertEqual(self.tenant.pii_denylist, before_deny)
        self.doc.refresh_from_db()
        self.assertIn("[ACCOUNT_105]", self.doc.markdown)

    def test_second_run_is_noop(self):
        sweep_tenant(self.tenant)
        self.tenant.refresh_from_db()

        second = sweep_tenant(self.tenant)
        self.assertEqual(second["junk"], 0)
        self.assertEqual(second["healed_rows"], 0)
        self.assertEqual(second["denied"], 0)
        self.assertEqual(second["deleted"], 0)
        self.assertEqual(second["examined"], 4)  # only keepers remain

    def test_max_entries_caps_work(self):
        # Cap below the junk count — only the first slice is examined.
        result = sweep_tenant(self.tenant, dry_run=True, max_entries=3)
        self.assertEqual(result["examined"], 3)


class RegistryJsonPathHealTests(_TenantMixin, TestCase):
    def test_real_registered_json_store_heals_through_json_text_narrowing(self):
        tenant = self._make_tenant(
            username="real-json-heal",
            entity_map={"[ACCOUNT_105]": {"name": "2026-05-30"}},
        )
        entry = JournalEntry.objects.create(
            tenant=tenant,
            date=date(2026, 8, 8),
            mood="steady",
            energy=JournalEntry.Energy.MEDIUM,
            wins=["Due [ACCOUNT_105]", "Escaped \\[ACCOUNT_105\\]"],
            challenges=["Unchanged"],
            reflection="",
            raw_text="no bracket in a flat field",
            pii_receipts={
                "wins": {
                    "state": "placeholder",
                    "redactions": [{"placeholder": "[ACCOUNT_105]"}],
                }
            },
        )

        result = sweep_tenant(tenant)

        entry.refresh_from_db()
        tenant.refresh_from_db()
        self.assertEqual(result["healed_rows"], 1)
        self.assertEqual(entry.wins, ["Due 2026-05-30", "Escaped 2026-05-30"])
        self.assertEqual(entry.challenges, ["Unchanged"])
        self.assertEqual(entry.pii_receipts["wins"]["redactions"], [])
        self.assertNotIn("[ACCOUNT_105]", tenant.pii_entity_map)

    def test_json_path_heal_round_trip_rewrites_wildcard_leaves_and_receipt(self):
        tenant = self._make_tenant(
            username="json-heal",
            entity_map={"[ACCOUNT_105]": {"name": "2026-05-30"}},
        )
        goal = Goal.objects.create(
            tenant=tenant,
            title="JSON carrier",
            target={
                "summary": "Due [ACCOUNT_105]",
                "items": [
                    {"note": "Plain [ACCOUNT_105]"},
                    {"note": "Escaped \\[ACCOUNT_105\\]"},
                ],
                "unregistered": "Keep raw 2026-05-30",
            },
            pii_receipts={
                "target": {
                    "state": "placeholder",
                    "redactions": [{"placeholder": "[ACCOUNT_105]"}],
                }
            },
        )
        synthetic = PlaceholderStore(
            model_label="journal.Goal",
            flat_fields=(),
            json_paths=("target.summary", "target.items[].note"),
            receipts_field="pii_receipts",
        )

        with patch("apps.pii.store_registry.registered_stores", return_value=(synthetic,)):
            result = sweep_tenant(tenant)

        goal.refresh_from_db()
        tenant.refresh_from_db()
        self.assertEqual(result["healed_rows"], 1)
        self.assertEqual(goal.target["summary"], "Due 2026-05-30")
        self.assertEqual(
            goal.target["items"],
            [{"note": "Plain 2026-05-30"}, {"note": "Escaped 2026-05-30"}],
        )
        self.assertEqual(goal.target["unregistered"], "Keep raw 2026-05-30")
        self.assertEqual(goal.pii_receipts["target"]["redactions"], [])
        self.assertNotIn("[ACCOUNT_105]", tenant.pii_entity_map)

    def test_mixed_flat_and_json_store_heals_from_either_side(self):
        """A store carrying both kinds of field must reach a token in either.

        The flat arm regressed once: any store with ``json_paths`` lost its
        ``__contains="["`` narrowing wholesale, so flat columns were only ever
        found by a full per-tenant scan.
        """
        synthetic = PlaceholderStore(
            model_label="journal.Goal",
            flat_fields=("title",),
            json_paths=("target.summary",),
            receipts_field="pii_receipts",
        )
        tenant = self._make_tenant(
            username="mixed-heal",
            entity_map={"[ACCOUNT_105]": {"name": "2026-05-30"}},
        )
        flat_only = Goal.objects.create(tenant=tenant, title="Due [ACCOUNT_105]", target={})
        json_only = Goal.objects.create(
            tenant=tenant,
            title="no token here",
            target={"summary": "Due [ACCOUNT_105]"},
        )

        with patch("apps.pii.store_registry.registered_stores", return_value=(synthetic,)):
            result = sweep_tenant(tenant)

        flat_only.refresh_from_db()
        json_only.refresh_from_db()
        self.assertEqual(result["healed_rows"], 2)
        self.assertEqual(flat_only.title, "Due 2026-05-30")
        self.assertEqual(json_only.target["summary"], "Due 2026-05-30")

    def test_flat_narrowing_predicate_survives_json_paths(self):
        """Pin the SQL: the flat bracket predicate must still be emitted."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        synthetic = PlaceholderStore(
            model_label="journal.Goal",
            flat_fields=("title",),
            json_paths=("target.summary",),
            receipts_field="pii_receipts",
        )
        tenant = self._make_tenant(
            username="narrow-sql",
            entity_map={"[ACCOUNT_105]": {"name": "2026-05-30"}},
        )
        Goal.objects.create(tenant=tenant, title="Due [ACCOUNT_105]", target={})

        with (
            patch("apps.pii.store_registry.registered_stores", return_value=(synthetic,)),
            CaptureQueriesContext(connection) as captured,
        ):
            sweep_tenant(tenant)

        goal_selects = [
            q["sql"] for q in captured.captured_queries if "journal_goals" in q["sql"] and "SELECT" in q["sql"]
        ]
        self.assertTrue(goal_selects, "expected a SELECT against the goal store")
        self.assertTrue(
            any("title" in sql and "LIKE" in sql.upper() for sql in goal_selects),
            f"flat-field bracket narrowing missing from: {goal_selects}",
        )

    def test_tombstoned_binding_is_never_healed_or_deleted(self):
        placeholder = "[PERSON_999]"
        tenant = self._make_tenant(
            username="retired-heal",
            entity_map={placeholder: {"name": "### machine text", "retired": True}},
        )
        goal = Goal.objects.create(tenant=tenant, title=f"Keep {placeholder}")

        result = sweep_tenant(tenant)

        goal.refresh_from_db()
        tenant.refresh_from_db()
        self.assertEqual(result["junk"], 0)
        self.assertEqual(result["healed_rows"], 0)
        self.assertEqual(result["deleted"], 0)
        # Retirement volume is its own counter, not folded in with keepers.
        self.assertEqual(result["retired_skipped"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(goal.title, f"Keep {placeholder}")
        self.assertIn(placeholder, tenant.pii_entity_map)


class SweepAllTenantsTests(_TenantMixin, TestCase):
    def test_per_tenant_error_isolation(self):
        good = self._make_tenant(username="good", entity_map=dict(JUNK_MAP))
        bad = self._make_tenant(username="bad", entity_map=dict(JUNK_MAP))

        original = junk_sweep.sweep_tenant

        def _side_effect(tenant, **kwargs):
            if tenant.pk == bad.pk:
                raise RuntimeError("kaboom")
            return original(tenant, **kwargs)

        with patch.object(junk_sweep, "sweep_tenant", side_effect=_side_effect):
            totals = sweep_all_tenants()

        self.assertEqual(totals["tenants_seen"], 2)
        self.assertEqual(totals["errors"], 1)
        self.assertEqual(totals["tenants_with_junk"], 1)

        # The good tenant was still cleaned despite the bad tenant raising.
        good.refresh_from_db()
        for placeholder in JUNK_MAP:
            self.assertNotIn(placeholder, good.pii_entity_map)
        # The failing tenant is left untouched (no partial write).
        bad.refresh_from_db()
        self.assertIn("[CREDIT_CARD_106]", bad.pii_entity_map)

    def test_skips_inactive_tenants(self):
        self._make_tenant(username="suspended", entity_map=dict(JUNK_MAP), status="suspended")
        totals = sweep_all_tenants()
        self.assertEqual(totals["tenants_seen"], 0)
