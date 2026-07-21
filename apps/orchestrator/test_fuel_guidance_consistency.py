"""Keep the three active create-plan guidance surfaces aligned."""

from pathlib import Path

from django.test import SimpleTestCase

_ROOT = Path(__file__).resolve().parents[2]
_GUIDANCE_FILES = (
    _ROOT / "runtime/openclaw/plugins/nbhd-fuel-tools/index.js",
    _ROOT / "templates/openclaw/rules/fuel.md",
    _ROOT / "templates/openclaw/docs/tools-reference.md",
)
_LOAD_BEARING_PHRASES = (
    "tenant-local start anchor",
    "MUST include today's weekday",
    "first_workout_date",
    "fallback behavior",
    "not a recommendation",
)


class FuelCreatePlanGuidanceConsistencyTests(SimpleTestCase):
    def test_create_plan_guidance_surfaces_keep_start_anchor_rules(self):
        for path in _GUIDANCE_FILES:
            content = path.read_text()
            with self.subTest(path=path.relative_to(_ROOT)):
                for phrase in _LOAD_BEARING_PHRASES:
                    self.assertIn(phrase, content)
