from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.pii.egress import _compile_known_value_matcher, redact_known_values


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
