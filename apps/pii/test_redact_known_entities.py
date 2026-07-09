"""Tests for ``redact_known_entities`` — the reuse-only substitution helper.

This path masks ONLY PII the tenant map already knows and mints nothing, so it
is safe for agent-authored text (platform issue reports) where minting would
pollute the map. These are pure-function tests; a ``SimpleNamespace`` stub is
enough because the helper only reads ``pii_entity_map`` / ``pii_denylist`` off
the tenant and never touches the DB.
"""

from __future__ import annotations

from types import SimpleNamespace

from django.test import TestCase

from apps.pii.entity_registry import normalize_denylist_key
from apps.pii.redactor import redact_known_entities


def _tenant(entity_map: dict, denylist: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(pii_entity_map=entity_map, pii_denylist=denylist or {})


class RedactKnownEntitiesTest(TestCase):
    def test_multi_entity_substitution(self):
        """Every known value is swapped for its placeholder in one pass."""
        tenant = _tenant(
            {
                "[PERSON_1]": {"name": "Jay Haughton"},
                "[GPE_1]": {"name": "Tokyo"},
            }
        )
        out = redact_known_entities(tenant, "Jay Haughton flew to Tokyo")
        self.assertEqual(out, "[PERSON_1] flew to [GPE_1]")

    def test_longest_match_first(self):
        """'Jay Haughton' must win over 'Jay' so the longer name isn't
        corrupted into '[PERSON_2] Haughton'."""
        tenant = _tenant(
            {
                "[PERSON_1]": {"name": "Jay Haughton"},
                "[PERSON_2]": {"name": "Jay"},
            }
        )
        out = redact_known_entities(tenant, "Jay Haughton met Jay")
        self.assertEqual(out, "[PERSON_1] met [PERSON_2]")

    def test_legacy_string_shaped_entries(self):
        """Legacy ``{placeholder: "Name"}`` string entries are coerced by
        entity_registry and still drive substitution."""
        tenant = _tenant({"[PERSON_1]": "Nana"})
        out = redact_known_entities(tenant, "Nana came over today")
        self.assertEqual(out, "[PERSON_1] came over today")

    def test_never_mints_unknown_values(self):
        """Unknown names are left verbatim — no new placeholder is coined."""
        tenant = _tenant({"[PERSON_1]": {"name": "Nana"}})
        out = redact_known_entities(tenant, "Bob visited Nana")
        self.assertEqual(out, "Bob visited [PERSON_1]")
        self.assertIn("Bob", out)
        self.assertNotIn("[PERSON_2]", out)

    def test_denylisted_entry_skipped(self):
        """A false-positive the user cleared (denylist) stops driving
        redaction even though its placeholder remains in the map."""
        tenant = _tenant(
            {"[PERSON_1]": {"name": "Nana"}},
            denylist={normalize_denylist_key("Nana"): {"reason": "false_positive"}},
        )
        out = redact_known_entities(tenant, "Nana came over today")
        self.assertEqual(out, "Nana came over today")

    def test_case_insensitive(self):
        tenant = _tenant({"[PERSON_1]": {"name": "Nana"}})
        out = redact_known_entities(tenant, "nana and NANA")
        self.assertEqual(out, "[PERSON_1] and [PERSON_1]")

    def test_does_not_rewrite_placeholder_interior(self):
        """A stored name that happens to appear inside an existing placeholder
        must not corrupt it."""
        tenant = _tenant({"[PERSON_1]": {"name": "Person"}})
        out = redact_known_entities(tenant, "See [PERSON_1] and Person")
        self.assertEqual(out, "See [PERSON_1] and [PERSON_1]")

    def test_empty_and_none_guards(self):
        tenant = _tenant({"[PERSON_1]": {"name": "Nana"}})
        self.assertEqual(redact_known_entities(tenant, ""), "")
        self.assertEqual(redact_known_entities(None, "Nana"), "Nana")
        self.assertEqual(redact_known_entities(_tenant({}), "Nana"), "Nana")
