"""Drift guard for the nbhd-fuel-tools set-contract tool schema (#593).

Phase 3 made the per-set ``type`` discriminator a required enum in
``nbhd_fuel_log_workout``. The enum values are owned by
``apps.fuel.set_contract`` (Python). This test pins the JS plugin schema
and ties it to the Python contract so the two can't drift apart — a
silent mismatch would let the model emit a shape the backend rejects.

Schema verified at merge time against ``npm pack openclaw@2026.5.7``:
OpenClaw validates tool input via ajv (``dist/schema-validator-*.js``)
and forwards ``parameters`` to the provider function schema
(``dist/openai-transport-stream-*.js``). ``enum`` + ``required`` are
core JSON-Schema keywords honored by every ajv config and provider —
none of the redactor-masked-failure vectors (see
``feedback_openclaw_config_schema_check.md``).
"""

from pathlib import Path

from django.test import SimpleTestCase

from apps.fuel.set_contract import SET_METRICS

_PLUGIN = Path(__file__).resolve().parents[2] / "runtime/openclaw/plugins/nbhd-fuel-tools/index.js"


class FuelToolSchemaShapeTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.src = _PLUGIN.read_text()

    def test_plugin_file_present(self):
        self.assertTrue(_PLUGIN.exists(), _PLUGIN)
        self.assertIn('name: "nbhd_fuel_log_workout"', self.src)

    def test_set_item_requires_type(self):
        # The per-set object must hard-require `type`.
        self.assertIn('required: ["type"]', self.src)

    def test_enum_matches_backend_contract(self):
        # The exact JS enum literal Phase 3 introduced...
        self.assertIn(
            'enum: ["weighted_reps", "bodyweight_reps", "hold_time"]',
            self.src,
        )
        # ...must equal the Python source of truth. If SET_METRICS ever
        # changes, this fails and forces updating the JS schema too.
        self.assertEqual(
            {"weighted_reps", "bodyweight_reps", "hold_time"},
            set(SET_METRICS),
        )

    def test_no_separate_skills_array_reintroduced(self):
        # Reconciled onto exercises[] only — guard against a `skills`
        # array creeping back into the tool schema.
        self.assertNotIn("skills:", self.src)
