"""Fuel plugin schema, manifest, and catalog-description drift guards."""

from __future__ import annotations

import json
import re
from pathlib import Path

from django.test import SimpleTestCase

from apps.fuel import catalog
from apps.fuel.set_contract import SET_METRICS

_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "runtime/openclaw/plugins/nbhd-fuel-tools"
_PLUGIN = _PLUGIN_DIR / "index.js"


class FuelToolSchemaShapeTests(SimpleTestCase):
    """Preserve the original typed-set contract assertions from Phase 3."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.src = _PLUGIN.read_text()

    def test_plugin_file_present(self):
        self.assertTrue(_PLUGIN.exists(), _PLUGIN)
        self.assertIn('name: "nbhd_fuel_log_workout"', self.src)

    def test_set_item_requires_type(self):
        self.assertIn('required: ["type"]', self.src)

    def test_enum_matches_backend_contract(self):
        self.assertIn(
            'enum: ["weighted_reps", "bodyweight_reps", "hold_time"]',
            self.src,
        )
        self.assertEqual(
            {"weighted_reps", "bodyweight_reps", "hold_time"},
            set(SET_METRICS),
        )

    def test_no_separate_skills_array_reintroduced(self):
        self.assertNotIn("skills:", self.src)


class FuelToolManifestTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = _PLUGIN.read_text()
        cls.manifest = json.loads((_PLUGIN_DIR / "openclaw.plugin.json").read_text())

    def test_manifest_contracts_exactly_the_registered_tools(self):
        registered = set(re.findall(r'name:\s*"(nbhd_fuel_[a-z_]+)"', self.source))
        self.assertEqual(set(self.manifest["contracts"]["tools"]), registered)

    def test_search_description_facets_match_backend_catalog(self):
        marker = 'name: "nbhd_fuel_search_exercises"'
        section = self.source[self.source.index(marker) :]
        description = re.search(r'description:\s*\n\s*"([^"]+)"', section).group(1)
        self.assertLessEqual(len(description.split()), 90)
        self.assertIn(f"Muscles: {', '.join(catalog.muscles())}.", description)
        self.assertIn(f"Equipment: {', '.join(catalog.equipment_types())}.", description)
        self.assertIn("use the returned name verbatim", description)
