"""Rendered-prompt pins for the always-loaded schedule-change directive."""

from django.test import TestCase, override_settings

from apps.orchestrator.personas import render_workspace_files
from apps.orchestrator.test_reminder_capability import MaximalTenantBudgetTest

_WEEK_AHEAD_DIRECTIVE = (
    "On travel or a major schedule change, search for cron and adjust affected jobs before they next run."
)


@override_settings(GRAVITY_ENABLED=True)
class WeekAheadDirectiveTests(TestCase):
    def test_renders_exactly_for_lean_and_all_gates(self):
        budget_case = MaximalTenantBudgetTest(methodName="test_maximal_render_stays_under_the_cap")
        all_gates_tenant = budget_case._tenant(all_gates=True)

        for shape, tenant in (("lean", None), ("all-gates", all_gates_tenant)):
            rendered = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
            with self.subTest(shape=shape):
                self.assertIn(_WEEK_AHEAD_DIRECTIVE, rendered)
