"""Pin always-loaded reply-marker guidance and insights tool descriptions."""

from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from apps.orchestrator.test_reminder_capability import MaximalTenantBudgetTest

_ROOT = Path(__file__).resolve().parents[2]
_INSIGHTS_PLUGIN = _ROOT / "runtime/openclaw/plugins/nbhd-insights-tools/index.js"
_LOAD_BEARING_PHRASES = (
    "Only mark a single, evidence-backed observation you believe; do not mark questions, generic advice, or "
    "tentative patterns.",
)
_CHANNEL_BEHAVIOR = (
    "Insight markers fire on the app, Telegram, and LINE; quick replies on the app only; charts only on Telegram/LINE."
)


def _tool_description(source: str, tool_name: str) -> str:
    start = source.index(f'name: "{tool_name}"')
    return source[start : source.index("parameters:", start)]


@override_settings(GRAVITY_ENABLED=True)
class ReplyMarkerAlwaysLoadedTests(TestCase):
    def test_quality_gate_and_channel_behavior_render_for_lean_and_all_gates(self):
        budget_case = MaximalTenantBudgetTest(methodName="test_maximal_render_stays_under_the_cap")
        all_gates_tenant = budget_case._tenant(all_gates=True)

        from apps.orchestrator.personas import render_workspace_files

        for shape, tenant in (("lean", None), ("all-gates", all_gates_tenant)):
            rendered = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
            with self.subTest(shape=shape):
                for phrase in _LOAD_BEARING_PHRASES:
                    self.assertIn(phrase, rendered)
                self.assertIn(_CHANNEL_BEHAVIOR, rendered)


class InsightsToolDescriptionTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = _INSIGHTS_PLUGIN.read_text()

    def test_record_description_keeps_marker_primary_path_and_fallback(self):
        description = _tool_description(self.source, "nbhd_insights_record")
        self.assertIn("inline in the reply as `[[insight:pillar/slug]]…[[/insight]]`", description)
        self.assertIn("direct tool only as a rare fallback", description)
        self.assertIn("if it's new, the registry creates a 'proposed' topic", description)

    def test_confirm_and_refute_descriptions_require_existing_evidence(self):
        confirm = _tool_description(self.source, "nbhd_insights_confirm")
        refute = _tool_description(self.source, "nbhd_insights_refute")
        self.assertIn("confirm an existing insight after evidence", confirm)
        self.assertIn("refute an existing insight after evidence", refute)
