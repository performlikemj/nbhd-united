"""Pin the always-loaded memory honesty directives."""

from django.test import TestCase, override_settings

from apps.orchestrator.personas import render_workspace_files
from apps.orchestrator.test_reminder_capability import MaximalTenantBudgetTest

_MEMORY_HONESTY_DIRECTIVES = (
    "For an unresolved [PERSON_n], [ORG_n], or [PLACE_n], say the name is redacted and ask who it is; "
    "never infer familiarity.",
    "Say you checked, searched, or found no record only after a lookup tool call this turn; otherwise say "
    "you have not checked.",
)


@override_settings(GRAVITY_ENABLED=True)
class MemoryHonestyDirectiveTest(TestCase):
    def test_lean_and_all_gates_renders_carry_exact_directives(self):
        budget_case = MaximalTenantBudgetTest(methodName="test_maximal_render_stays_under_the_cap")
        all_gates_tenant = budget_case._tenant(all_gates=True)

        self.assertEqual(sum(map(len, _MEMORY_HONESTY_DIRECTIVES)), 243)
        for shape, tenant in (("lean", None), ("all-gates", all_gates_tenant)):
            with self.subTest(shape=shape):
                prompt = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
                for directive in _MEMORY_HONESTY_DIRECTIVES:
                    self.assertIn(directive, prompt)
