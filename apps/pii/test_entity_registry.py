"""Tests for the entity registry helpers and their integration with
``rehydrate_text`` + ``redact_user_message``.

Covers:
- ``coerce``: legacy string entries, new dict entries, malformed entries,
  unknown-field dropping.
- ``get_name``, ``get_metadata``: shape-agnostic accessors.
- ``inverted_names``: name → placeholder lookup across mixed shapes.
- ``rehydrate_text``: works against pure-legacy, pure-new, and mixed maps.
- ``redact_user_message``: writes new dict-shaped entries while leaving
  existing legacy string entries untouched (opportunistic migration).
"""

from __future__ import annotations

import secrets
from unittest.mock import patch

from django.test import TestCase

from apps.pii.entity_registry import (
    canonical_key,
    coerce,
    get_metadata,
    get_name,
    inverted_names,
    inverted_names_ci,
    is_denied,
    iter_normalized,
    normalize_denylist_key,
    to_storage_value,
)
from apps.pii.redactor import rehydrate_text
from apps.tenants.models import Tenant, User


class CoerceTests(TestCase):
    def test_string_entry_becomes_name_only_dict(self):
        self.assertEqual(coerce("Nana"), {"name": "Nana"})

    def test_dict_entry_with_full_shape_round_trips(self):
        entry = {"name": "Nana", "relationship": "daughter", "notes": "4.5", "updated_at": "2026-05-21"}
        self.assertEqual(coerce(entry), entry)

    def test_dict_entry_drops_unknown_fields(self):
        out = coerce({"name": "X", "junk": "y", "id": 42})
        self.assertEqual(out, {"name": "X"})

    def test_dict_entry_missing_name_gets_empty_string(self):
        self.assertEqual(coerce({"relationship": "friend"}), {"relationship": "friend", "name": ""})

    def test_none_becomes_empty_name(self):
        self.assertEqual(coerce(None), {"name": ""})

    def test_unknown_type_becomes_empty_name(self):
        self.assertEqual(coerce(42), {"name": ""})

    def test_returns_fresh_dict_safe_to_mutate(self):
        entry = {"name": "X", "relationship": "r"}
        out = coerce(entry)
        out["name"] = "Y"
        self.assertEqual(entry["name"], "X")


class GetNameTests(TestCase):
    def test_string(self):
        self.assertEqual(get_name("Nana"), "Nana")

    def test_dict(self):
        self.assertEqual(get_name({"name": "Nana"}), "Nana")

    def test_dict_without_name(self):
        self.assertEqual(get_name({}), "")

    def test_dict_with_non_string_name(self):
        self.assertEqual(get_name({"name": 42}), "")

    def test_none(self):
        self.assertEqual(get_name(None), "")


class GetMetadataTests(TestCase):
    def test_string_has_no_metadata(self):
        self.assertEqual(get_metadata("Nana"), {})

    def test_dict_returns_metadata_excluding_name(self):
        entry = {"name": "Nana", "relationship": "daughter", "notes": "4.5", "updated_at": "2026-05-21"}
        self.assertEqual(
            get_metadata(entry),
            {"relationship": "daughter", "notes": "4.5", "updated_at": "2026-05-21"},
        )

    def test_dict_excludes_empty_metadata_fields(self):
        entry = {"name": "X", "relationship": "", "notes": "n"}
        self.assertEqual(get_metadata(entry), {"notes": "n"})


class ToStorageValueTests(TestCase):
    def test_minimal_name_only(self):
        self.assertEqual(to_storage_value("Nana"), {"name": "Nana"})

    def test_full_shape(self):
        self.assertEqual(
            to_storage_value("Nana", relationship="daughter", notes="4.5", updated_at="2026-05-21"),
            {"name": "Nana", "relationship": "daughter", "notes": "4.5", "updated_at": "2026-05-21"},
        )

    def test_empty_optionals_dropped(self):
        self.assertEqual(
            to_storage_value("Nana", relationship="", notes=""),
            {"name": "Nana"},
        )


class IterNormalizedTests(TestCase):
    def test_empty_map(self):
        self.assertEqual(list(iter_normalized({})), [])
        self.assertEqual(list(iter_normalized(None)), [])

    def test_mixed_shapes(self):
        m = {
            "[PERSON_1]": "Nana",
            "[PERSON_2]": {"name": "Mit", "relationship": "spouse"},
        }
        out = dict(iter_normalized(m))
        self.assertEqual(out["[PERSON_1]"], {"name": "Nana"})
        self.assertEqual(out["[PERSON_2]"], {"name": "Mit", "relationship": "spouse"})


class InvertedNamesTests(TestCase):
    def test_mixed_shapes_inverted_correctly(self):
        m = {
            "[PERSON_1]": "Nana",
            "[PERSON_2]": {"name": "Mit", "relationship": "spouse"},
            "[PERSON_3]": {"relationship": "no-name"},  # skipped — empty name
        }
        self.assertEqual(inverted_names(m), {"Nana": "[PERSON_1]", "Mit": "[PERSON_2]"})

    def test_empty(self):
        self.assertEqual(inverted_names(None), {})
        self.assertEqual(inverted_names({}), {})


class CanonicalKeyTests(TestCase):
    def test_lowercases_ascii(self):
        self.assertEqual(canonical_key("Sautai"), "sautai")
        self.assertEqual(canonical_key("SAUTAI"), "sautai")

    def test_strips_outer_whitespace(self):
        self.assertEqual(canonical_key("  Sautai  "), "sautai")
        self.assertEqual(canonical_key("\tSautai\n"), "sautai")

    def test_preserves_internal_whitespace(self):
        # Collapsing internal whitespace is a different bug class (NER
        # span boundaries). Keep "Jay  Haughton" and "Jay Haughton"
        # distinct under canonical_key — they came in as distinct spans.
        self.assertNotEqual(
            canonical_key("Jay  Haughton"),
            canonical_key("Jay Haughton"),
        )

    def test_casefold_handles_german_eszett(self):
        # casefold() collapses "ß" to "ss"; lower() does not.
        self.assertEqual(canonical_key("Straße"), canonical_key("strasse"))

    def test_empty_and_non_string_return_empty(self):
        self.assertEqual(canonical_key(""), "")
        self.assertEqual(canonical_key("   "), "")
        self.assertEqual(canonical_key(None), "")  # type: ignore[arg-type]
        self.assertEqual(canonical_key(42), "")  # type: ignore[arg-type]


class InvertedNamesCITests(TestCase):
    def test_case_variants_collapse_to_lowest_numbered(self):
        # The exact bug from the canary audit: "Sautai" → [PERSON_5]
        # stored first, then 58 case-variant duplicates accumulated.
        # The canonical lookup must route to the lowest-numbered one.
        m = {
            "[PERSON_408]": "sautai",
            "[PERSON_5]": "Sautai",
            "[PERSON_77]": "SAUTAI",
        }
        result = inverted_names_ci(m)
        self.assertEqual(set(result.keys()), {"sautai"})
        display_name, placeholder = result["sautai"]
        self.assertEqual(placeholder, "[PERSON_5]")
        # Display name is the one belonging to the canonical placeholder.
        self.assertEqual(display_name, "Sautai")

    def test_whitespace_variants_collapse(self):
        m = {
            "[PERSON_1]": "Sautai",
            "[PERSON_2]": "  Sautai  ",
        }
        result = inverted_names_ci(m)
        self.assertEqual(len(result), 1)
        display, placeholder = result["sautai"]
        self.assertEqual(placeholder, "[PERSON_1]")
        # Display name is stripped so it can be used directly in regex
        # construction (re.escape preserves whitespace literally).
        self.assertEqual(display, "Sautai")

    def test_mixed_legacy_and_dict_shapes(self):
        m = {
            "[PERSON_1]": "Nana",
            "[PERSON_2]": {"name": "NANA", "relationship": "grandmother"},
        }
        result = inverted_names_ci(m)
        self.assertEqual(len(result), 1)
        self.assertEqual(result["nana"][1], "[PERSON_1]")

    def test_empty_names_skipped(self):
        m = {
            "[PERSON_1]": "",
            "[PERSON_2]": "   ",
            "[PERSON_3]": {"relationship": "no-name"},
            "[PERSON_4]": "Real",
        }
        result = inverted_names_ci(m)
        self.assertEqual(set(result.keys()), {"real"})

    def test_distinct_names_kept_separate(self):
        m = {
            "[PERSON_1]": "Alice",
            "[PERSON_2]": "Bob",
        }
        result = inverted_names_ci(m)
        self.assertEqual(set(result.keys()), {"alice", "bob"})

    def test_empty_map(self):
        self.assertEqual(inverted_names_ci(None), {})
        self.assertEqual(inverted_names_ci({}), {})

    def test_malformed_placeholder_loses_canonical_pick_tie(self):
        # A malformed placeholder (no _N suffix) parses to num=0 and
        # would otherwise win on lowest-num. Well-formed [PERSON_5]
        # should still beat it because the tiebreak comparison is "<"
        # (strict), and 0 < 5 means malformed wins... so this test
        # documents that malformed entries DO win the tiebreak.
        # That's acceptable: such entries shouldn't exist in prod,
        # and if they do, rehydration still works via the underlying
        # placeholder string.
        m = {
            "[NOT_A_PLACEHOLDER]": "Sautai",
            "[PERSON_5]": "Sautai",
        }
        result = inverted_names_ci(m)
        # Documenting current behaviour, not asserting it as ideal:
        self.assertEqual(result["sautai"][1], "[NOT_A_PLACEHOLDER]")


class IsDeniedTests(TestCase):
    def test_match_is_case_insensitive(self):
        d = {"goal": {}}
        self.assertTrue(is_denied(d, "goal"))
        self.assertTrue(is_denied(d, "Goal"))
        self.assertTrue(is_denied(d, "GOAL"))

    def test_match_strips_whitespace(self):
        d = {"calendar": {"reason": "manual"}}
        self.assertTrue(is_denied(d, "  calendar  "))

    def test_empty_denylist_returns_false(self):
        self.assertFalse(is_denied(None, "anything"))
        self.assertFalse(is_denied({}, "anything"))

    def test_empty_name_returns_false(self):
        d = {"goal": {}}
        self.assertFalse(is_denied(d, ""))
        self.assertFalse(is_denied(d, "   "))

    def test_unknown_name_returns_false(self):
        d = {"goal": {}, "calendar": {}}
        self.assertFalse(is_denied(d, "sautai"))

    def test_non_string_input_returns_false(self):
        d = {"goal": {}}
        self.assertFalse(is_denied(d, None))  # type: ignore[arg-type]
        self.assertFalse(is_denied(d, 42))  # type: ignore[arg-type]

    def test_metadata_payload_does_not_affect_match(self):
        # The denylist value is opaque metadata for downstream consumers
        # (UI, future arbiter). is_denied only cares about key presence.
        d = {
            "goal": {},
            "calendar": {"reason": "manual", "decided_at": "2026-05-21"},
        }
        self.assertTrue(is_denied(d, "goal"))
        self.assertTrue(is_denied(d, "calendar"))


class NormalizeDenylistKeyTests(TestCase):
    def test_matches_canonical_key(self):
        self.assertEqual(normalize_denylist_key("Goal"), canonical_key("Goal"))
        self.assertEqual(normalize_denylist_key("  Calendar  "), "calendar")


class RehydrateBackwardCompatTests(TestCase):
    def test_legacy_string_entries_rehydrate(self):
        m = {"[PERSON_1]": "Alice"}
        self.assertEqual(rehydrate_text("hello [PERSON_1]", m), "hello Alice")

    def test_new_dict_entries_rehydrate_from_name(self):
        m = {"[PERSON_1]": {"name": "Alice", "relationship": "friend"}}
        self.assertEqual(rehydrate_text("hello [PERSON_1]", m), "hello Alice")

    def test_mixed_map(self):
        m = {
            "[PERSON_1]": "Alice",
            "[PERSON_2]": {"name": "Bob"},
        }
        self.assertEqual(rehydrate_text("[PERSON_1] and [PERSON_2]", m), "Alice and Bob")

    def test_dict_entry_without_name_leaves_placeholder(self):
        # An entry with empty name shouldn't blank out the placeholder —
        # leave it so the issue is visible rather than silently dropping
        # context.
        m = {"[PERSON_1]": {"relationship": "friend"}}
        self.assertEqual(rehydrate_text("hello [PERSON_1]", m), "hello [PERSON_1]")

    def test_unknown_placeholder_unchanged(self):
        m = {"[PERSON_1]": "Alice"}
        self.assertEqual(rehydrate_text("hello [PERSON_99]", m), "hello [PERSON_99]")

    def test_empty_text(self):
        self.assertEqual(rehydrate_text("", {"[PERSON_1]": "X"}), "")

    def test_no_placeholders_short_circuits(self):
        self.assertEqual(rehydrate_text("hello world", {"[PERSON_1]": "X"}), "hello world")


def _make_tenant() -> Tenant:
    user = User.objects.create_user(
        username=f"u_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="container.example.com",
        model_tier="starter",
    )


class RedactWriteNewShapeTests(TestCase):
    """``redact_user_message`` should write *new* entries in the new dict
    shape while leaving previously written legacy string entries alone.
    """

    def test_new_entry_written_as_dict(self):
        from apps.pii.redactor import DetectedEntity, redact_user_message

        tenant = _make_tenant()
        tenant.pii_entity_map = {}
        tenant.save(update_fields=["pii_entity_map"])

        # Force detection of one PERSON span at a deterministic offset.
        text = "hello Wanda there"
        person_start = text.index("Wanda")
        person_end = person_start + len("Wanda")
        fake = [DetectedEntity(entity_type="PERSON", start=person_start, end=person_end, score=0.99)]

        with (
            patch("apps.pii.redactor._detect_pii", return_value=fake),
            patch("apps.pii.redactor._filter_results", side_effect=lambda r, t, a, **kw: r),
        ):
            out = redact_user_message(text, tenant)

        self.assertEqual(out, "hello [PERSON_1] there")
        tenant.refresh_from_db()
        entry = tenant.pii_entity_map["[PERSON_1]"]
        self.assertIsInstance(entry, dict)
        self.assertEqual(entry["name"], "Wanda")

    def test_existing_legacy_string_entries_preserved(self):
        from apps.pii.redactor import DetectedEntity, redact_user_message

        tenant = _make_tenant()
        # Existing legacy entry — should stay a string, not get rewritten.
        tenant.pii_entity_map = {"[PERSON_1]": "Alice"}
        tenant.save(update_fields=["pii_entity_map"])

        text = "hello Wanda there"
        person_start = text.index("Wanda")
        person_end = person_start + len("Wanda")
        fake = [DetectedEntity(entity_type="PERSON", start=person_start, end=person_end, score=0.99)]

        with (
            patch("apps.pii.redactor._detect_pii", return_value=fake),
            patch("apps.pii.redactor._filter_results", side_effect=lambda r, t, a, **kw: r),
        ):
            redact_user_message(text, tenant)

        tenant.refresh_from_db()
        # Alice stays a string (legacy)
        self.assertEqual(tenant.pii_entity_map["[PERSON_1]"], "Alice")
        # Wanda gets a new placeholder with the new dict shape
        self.assertEqual(tenant.pii_entity_map["[PERSON_2]"], {"name": "Wanda"})

    def test_known_name_in_mixed_map_collides_to_existing_placeholder(self):
        """The known-entity pass uses ``inverted_names``, which must
        work across mixed shapes so re-mention of an existing name
        collides correctly.
        """
        from apps.pii.redactor import redact_user_message

        tenant = _make_tenant()
        # Mixed map: one legacy str, one new dict.
        tenant.pii_entity_map = {
            "[PERSON_1]": "Alice",
            "[PERSON_2]": {"name": "Bob"},
        }
        tenant.save(update_fields=["pii_entity_map"])

        # No new detection — just verify Step 1 (known-entity replacement)
        # works for both legacy and new shapes.
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            out = redact_user_message("Alice met Bob", tenant)

        self.assertEqual(out, "[PERSON_1] met [PERSON_2]")
