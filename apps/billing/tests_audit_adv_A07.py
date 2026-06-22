"""Adversarial-audit regression tests for cluster A07.

billing#2: the global platform kill-switch must be the operator-controlled
``MonthlyBudget.is_capped`` flag — an independent, durable switch decoupled
from the reconcile-driven ``spent_dollars`` counter. cap_budget --uncap must
NOT zero spent_dollars (which both wiped the true month-to-date figure and let
the next hourly reconcile silently re-cap when real spend was over budget).
"""

from datetime import date
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.tenants.services import create_tenant

from .models import MonthlyBudget
from .services import check_budget


class GlobalCapKillSwitchTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="A07 Cap Test", telegram_chat_id=444555777)
        self.first = date.today().replace(day=1)

    def test_is_capped_blocks_even_when_under_budget(self):
        """The operator kill-switch engages the global breaker independent of spend."""
        budget = MonthlyBudget.objects.create(
            month=self.first, budget_dollars=100, spent_dollars=5, is_capped=True
        )
        self.assertGreater(budget.remaining, 0)  # plenty of budget left
        self.assertEqual(check_budget(self.tenant), "global")

    def test_remaining_exhausted_still_blocks_without_capped(self):
        """The spend-exhaustion path keeps blocking on its own."""
        MonthlyBudget.objects.create(
            month=self.first, budget_dollars=100, spent_dollars=100, is_capped=False
        )
        self.assertEqual(check_budget(self.tenant), "global")

    def test_uncapped_under_budget_not_blocked(self):
        MonthlyBudget.objects.create(
            month=self.first, budget_dollars=100, spent_dollars=5, is_capped=False
        )
        self.assertEqual(check_budget(self.tenant), "")


class CapBudgetCommandTest(TestCase):
    def setUp(self):
        self.first = date.today().replace(day=1)

    def test_cap_sets_flag_without_touching_spend(self):
        budget = MonthlyBudget.objects.create(
            month=self.first, budget_dollars=100, spent_dollars=42
        )
        call_command("cap_budget", stdout=StringIO())
        budget.refresh_from_db()
        self.assertTrue(budget.is_capped)
        # True month-to-date spend figure is preserved, not overwritten.
        self.assertEqual(float(budget.spent_dollars), 42.0)

    def test_uncap_preserves_spend(self):
        """--uncap clears the flag but leaves spent_dollars intact.

        Previously --uncap zeroed spent_dollars, which both wiped the accurate
        month-to-date figure surfaced in the transparency UI and let the next
        hourly reconcile re-derive the spend. Now the spend column is untouched.
        """
        budget = MonthlyBudget.objects.create(
            month=self.first, budget_dollars=100, spent_dollars=150, is_capped=True
        )
        call_command("cap_budget", "--uncap", stdout=StringIO())
        budget.refresh_from_db()
        self.assertFalse(budget.is_capped)
        # Spend figure untouched (not wiped to $0).
        self.assertEqual(float(budget.spent_dollars), 150.0)

    def test_uncap_is_durable_against_reconcile_when_under_budget(self):
        """A flag-only cap then uncap survives the reconcile ratchet.

        Real spend within budget: capping no longer inflates spent_dollars to
        budget_dollars, so after --uncap the reconcile counter is unchanged and
        the tenant is not silently re-blocked. (Decoupling the kill-switch from
        the spend counter is the substance of the fix.)
        """
        MonthlyBudget.objects.create(
            month=self.first, budget_dollars=100, spent_dollars=30
        )
        tenant = create_tenant(display_name="A07 Uncap", telegram_chat_id=444555888)

        call_command("cap_budget", stdout=StringIO())
        self.assertEqual(check_budget(tenant), "global")

        call_command("cap_budget", "--uncap", stdout=StringIO())
        budget = MonthlyBudget.objects.get(month=self.first)
        # Cap never overwrote the true spend, so uncap leaves it well under budget.
        self.assertEqual(float(budget.spent_dollars), 30.0)
        self.assertEqual(check_budget(tenant), "")
