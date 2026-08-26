"""Keep the three active create-plan guidance surfaces aligned."""

from pathlib import Path

from django.test import SimpleTestCase

_ROOT = Path(__file__).resolve().parents[2]
_GUIDANCE_FILES = (
    _ROOT / "runtime/openclaw/plugins/nbhd-fuel-tools/index.js",
    _ROOT / "templates/openclaw/rules/fuel.md",
    _ROOT / "templates/openclaw/docs/tools-reference.md",
)
_DISCOVERY_FILES = (
    _ROOT / "templates/openclaw/AGENTS.md",
    _ROOT / "templates/openclaw/rules/fuel.md",
    _ROOT / "templates/openclaw/docs/tools-reference.md",
)
_DISCOVERY_GATE = (
    "For any workout plan or fill-in, first `tool_search` for `nbhd_fuel_search_exercises` by exact name "
    "and call it for each accessory/mobility group; then find and call "
    "`nbhd_fuel_create_plan`/`nbhd_fuel_update_plan`."
)
_LOAD_BEARING_PHRASES = (
    "tenant-local start anchor",
    "MUST include today's weekday",
    "first_workout_date",
    "fallback behavior",
    "not a recommendation",
    "nbhd_fuel_search_exercises",
    "use the returned name verbatim",
    "complete day object",
    "catalog-named skills with hold_time sets",
    "blocks only for non-movement work",
    "never swap a user-requested movement without asking",
    "accessory_rotations",
)


class FuelCreatePlanGuidanceConsistencyTests(SimpleTestCase):
    def test_create_plan_guidance_surfaces_keep_start_anchor_rules(self):
        for path in _GUIDANCE_FILES:
            content = path.read_text()
            with self.subTest(path=path.relative_to(_ROOT)):
                for phrase in _LOAD_BEARING_PHRASES:
                    self.assertIn(phrase, content)

    def test_always_loaded_discovery_gate_and_on_demand_copies_match_exactly(self):
        for path in _DISCOVERY_FILES:
            with self.subTest(path=path.relative_to(_ROOT)):
                self.assertIn(_DISCOVERY_GATE, path.read_text())

    def test_rendered_agents_snapshot_contains_the_search_before_write_gate(self):
        from apps.orchestrator.personas import render_agents_md

        self.assertIn(_DISCOVERY_GATE, render_agents_md("neighbor"))

    def test_both_write_tool_descriptions_name_the_search_tool(self):
        plugin = (_ROOT / "runtime/openclaw/plugins/nbhd-fuel-tools/index.js").read_text()
        for tool_name in ("nbhd_fuel_create_plan", "nbhd_fuel_update_plan"):
            start = plugin.index(f'name: "{tool_name}"')
            description = plugin[start : plugin.index("parameters:", start)]
            self.assertIn("First call nbhd_fuel_search_exercises", description)
