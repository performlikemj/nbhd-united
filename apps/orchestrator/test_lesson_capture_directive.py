"""Pin the always-loaded durable-lesson capture directive."""

from django.test import TestCase, override_settings

from apps.orchestrator.personas import render_workspace_files
from apps.orchestrator.test_reminder_capability import MaximalTenantBudgetTest

_LESSON_CAPTURE_DIRECTIVE = (
    "When the user states a durable personal lesson, search nbhd_lesson_search, then call "
    "nbhd_lesson_suggest; say it was added to their constellation."
)


@override_settings(GRAVITY_ENABLED=True)
class LessonCaptureDirectiveTest(TestCase):
    def test_lean_and_all_gates_renders_carry_exact_directive(self):
        budget_case = MaximalTenantBudgetTest(methodName="test_maximal_render_stays_under_the_cap")
        all_gates_tenant = budget_case._tenant(all_gates=True)

        for shape, tenant in (("lean", None), ("all-gates", all_gates_tenant)):
            with self.subTest(shape=shape):
                prompt = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
                self.assertIn(_LESSON_CAPTURE_DIRECTIVE, prompt)
