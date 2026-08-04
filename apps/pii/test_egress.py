from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.pii.egress import (
    _compile_known_value_matcher,
    redact_known_value_fields,
    redact_known_values,
)


def _tenant(entity_map):
    return SimpleNamespace(id="tenant-a", pk="tenant-a", pii_entity_map=entity_map)


class KnownValueEgressGuardTests(SimpleTestCase):
    def setUp(self):
        _compile_known_value_matcher.cache_clear()

    def test_boundaries_longest_first_short_values_and_placeholder_interior(self):
        tenant = _tenant(
            {
                "[PERSON_1]": {"name": "Theo Smith"},
                "[PERSON_2]": {"name": "Theo"},
                "[PERSON_3]": {"name": "Li"},
            }
        )
        self.assertEqual(
            redact_known_values(
                tenant,
                "Theo Smith met Theo; Theodor and Li stayed. [PERSON_2]",
                seam="test",
            ),
            "[PERSON_1] met [PERSON_2]; Theodor and Li stayed. [PERSON_2]",
        )

    def test_cache_reused_and_invalidated_when_map_changes(self):
        tenant = _tenant({"[PERSON_1]": "Theo"})
        self.assertEqual(redact_known_values(tenant, "Theo", seam="test"), "[PERSON_1]")
        first = _compile_known_value_matcher.cache_info()
        self.assertEqual(redact_known_values(tenant, "Theo", seam="test"), "[PERSON_1]")
        second = _compile_known_value_matcher.cache_info()
        self.assertEqual(second.hits, first.hits + 1)

        tenant.pii_entity_map = {"[PERSON_1]": "Theo", "[ORG_1]": "Optiver"}
        self.assertEqual(redact_known_values(tenant, "Optiver", seam="test"), "[ORG_1]")
        third = _compile_known_value_matcher.cache_info()
        self.assertEqual(third.misses, second.misses + 1)

    def test_error_returns_original_and_logs_structured_warning(self):
        tenant = _tenant({"[PERSON_1]": "Theo"})
        with (
            patch("apps.pii.egress._matcher_for_tenant", side_effect=RuntimeError("boom")),
            self.assertLogs("apps.pii.egress", level="WARNING") as logs,
        ):
            original = "Theo stays available"
            self.assertEqual(redact_known_values(tenant, original, seam="unit_failure"), original)
        self.assertIn("pii_egress_guard_error tenant=tenant-a seam=unit_failure", logs.output[0])

    def test_recursive_guard_only_changes_allowlisted_human_text_fields(self):
        tenant = _tenant({"[PERSON_1]": "Theo Smith", "[ORG_1]": "Optiver"})
        payload = {
            "id": "Theo Smith-id",
            "status": "Optiver",
            "slug": "theo-smith-optiver",
            "date": "2026-08-04",
            "amount": "123.45",
            "title": "Theo Smith plan",
            "rows": [
                {
                    "description": "Meet Theo Smith at Optiver",
                    "kind": "Theo Smith",
                    "account_id": "Optiver-42",
                }
            ],
            "matched_tokens": ["Theo Smith", "Optiver"],
        }
        guarded = redact_known_value_fields(
            tenant,
            payload,
            seam="fixture",
            text_fields=frozenset({"title", "description", "matched_tokens"}),
        )
        self.assertEqual(set(guarded), set(payload))
        self.assertEqual(set(guarded["rows"][0]), set(payload["rows"][0]))
        self.assertEqual(guarded["title"], "[PERSON_1] plan")
        self.assertEqual(guarded["rows"][0]["description"], "Meet [PERSON_1] at [ORG_1]")
        self.assertEqual(guarded["matched_tokens"], ["[PERSON_1]", "[ORG_1]"])
        for field in ("id", "status", "slug", "date", "amount"):
            self.assertEqual(guarded[field], payload[field])
        self.assertEqual(guarded["rows"][0]["kind"], payload["rows"][0]["kind"])
        self.assertEqual(guarded["rows"][0]["account_id"], payload["rows"][0]["account_id"])

    def test_recursive_guard_error_returns_original_and_logs(self):
        tenant = _tenant({"[PERSON_1]": "Theo Smith"})
        payload = {"title": "Theo Smith", "id": "unchanged"}
        with (
            patch("apps.pii.egress._redact_known_values", side_effect=RuntimeError("boom")),
            self.assertLogs("apps.pii.egress", level="WARNING") as logs,
        ):
            self.assertIs(
                redact_known_value_fields(
                    tenant,
                    payload,
                    seam="recursive_failure",
                    text_fields=frozenset({"title"}),
                ),
                payload,
            )
        self.assertIn("pii_egress_guard_error tenant=tenant-a seam=recursive_failure", logs.output[0])
