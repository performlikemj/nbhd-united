"""Keep the three active create-plan guidance surfaces aligned."""

from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from apps.orchestrator.test_reminder_capability import MaximalTenantBudgetTest

_ROOT = Path(__file__).resolve().parents[2]
_GUIDANCE_FILES = (
    _ROOT / "runtime/openclaw/plugins/nbhd-fuel-tools/index.js",
    _ROOT / "templates/openclaw/docs/tools-reference.md",
)
_DISCOVERY_FILES = (
    _ROOT / "templates/openclaw/AGENTS.md",
    _ROOT / "templates/openclaw/rules/fuel.md",
    _ROOT / "templates/openclaw/docs/tools-reference.md",
)
_DISCOVERY_GATE = (
    "For Fuel plans/fill-ins, first use `tool_search` for exact `nbhd_fuel_search_exercises` and call it "
    "per accessory/mobility group; then find/call `nbhd_fuel_create_plan`/`nbhd_fuel_update_plan`. Plans "
    "four weeks or longer rotate accessories every 1–2 weeks."
)
_CHAT_ONLY_PLAN_GATE = (
    'Exception: creating/building a workout plan is a Fuel WRITE, not "planning" — find and call '
    "`nbhd_fuel_create_plan` that same turn; never deliver a chat-only plan."
)
_PLAN_CONTRACT_PHRASES = (
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
    "four weeks or longer",
)
_LOAD_BEARING_PHRASES = (
    "A reported sleep duration or quality is a Fuel event: call nbhd_fuel_log_sleep this turn and briefly confirm.",
)


class FuelCreatePlanGuidanceConsistencyTests(SimpleTestCase):
    def test_create_plan_guidance_surfaces_keep_start_anchor_rules(self):
        for path in _GUIDANCE_FILES:
            content = path.read_text()
            with self.subTest(path=path.relative_to(_ROOT)):
                for phrase in _PLAN_CONTRACT_PHRASES:
                    self.assertIn(phrase, content)

    def test_always_loaded_discovery_gate_and_on_demand_copies_match_exactly(self):
        for path in _DISCOVERY_FILES:
            with self.subTest(path=path.relative_to(_ROOT)):
                self.assertIn(_DISCOVERY_GATE, path.read_text())

    def test_rendered_agents_snapshot_contains_the_search_before_write_gate(self):
        from apps.orchestrator.personas import render_agents_md

        rendered = render_agents_md("neighbor")
        self.assertIn(_CHAT_ONLY_PLAN_GATE, rendered)
        self.assertIn(_DISCOVERY_GATE, rendered)
        self.assertLess(rendered.index(_CHAT_ONLY_PLAN_GATE), rendered.index(_DISCOVERY_GATE))

    def test_agents_opening_keeps_internals_invisible_wording(self):
        agents = (_ROOT / "templates/openclaw/AGENTS.md").read_text()
        self.assertIn(
            "You are a personal AI assistant on NBHD United. Your user is a regular person, not a developer.\n"
            "They should never have to think about files, configs, or how you work. It just works.",
            agents,
        )

    def test_both_write_tool_descriptions_name_the_search_tool(self):
        plugin = (_ROOT / "runtime/openclaw/plugins/nbhd-fuel-tools/index.js").read_text()
        for tool_name in ("nbhd_fuel_create_plan", "nbhd_fuel_update_plan"):
            start = plugin.index(f'name: "{tool_name}"')
            description = plugin[start : plugin.index("parameters:", start)]
            self.assertIn("First call nbhd_fuel_search_exercises", description)
            self.assertIn("four weeks or longer", description)


@override_settings(GRAVITY_ENABLED=True)
class FuelAlwaysLoadedGuidanceTests(TestCase):
    def test_sleep_write_gate_renders_for_lean_and_all_gates(self):
        budget_case = MaximalTenantBudgetTest(methodName="test_maximal_render_stays_under_the_cap")
        all_gates_tenant = budget_case._tenant(all_gates=True)

        from apps.orchestrator.personas import render_workspace_files

        for shape, tenant in (("lean", None), ("all-gates", all_gates_tenant)):
            rendered = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
            with self.subTest(shape=shape):
                for phrase in _LOAD_BEARING_PHRASES:
                    self.assertIn(phrase, rendered)
