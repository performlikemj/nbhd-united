"""Tests for the RuntimeReconcileScanView endpoint."""

from __future__ import annotations

from datetime import date as _date
from decimal import Decimal

from django.test import TestCase
from django.test.utils import override_settings

from apps.fuel.models import BodyWeightLog, Workout, WorkoutCategory, WorkoutStatus
from apps.journal.models import Goal, Task
from apps.tenants.services import create_tenant


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeReconcileScanViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Scan Tenant", telegram_chat_id=131313)
        self.other_tenant = create_tenant(display_name="Other Tenant", telegram_chat_id=141414)
        self.tenant.finance_enabled = True
        self.tenant.fuel_enabled = True
        self.tenant.save(update_fields=["finance_enabled", "fuel_enabled"])

    def _url(self, tenant_id=None):
        tid = tenant_id or self.tenant.id
        return f"/api/v1/integrations/runtime/{tid}/reconcile/scan/"

    def _headers(self, tenant_id=None, key="shared-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": str(tenant_id or self.tenant.id),
        }

    # ── auth + validation ───────────────────────────────────────────

    def test_requires_internal_auth(self):
        response = self.client.get(self._url(), {"claim": "paid the card"})
        self.assertEqual(response.status_code, 401)

    def test_missing_claim_returns_400(self):
        response = self.client.get(self._url(), **self._headers())
        self.assertEqual(response.status_code, 400)

    def test_empty_claim_returns_400(self):
        response = self.client.get(self._url(), {"claim": "  "}, **self._headers())
        self.assertEqual(response.status_code, 400)

    # ── goals ────────────────────────────────────────────────────────

    def test_matches_active_goal_by_title_token(self):
        goal = Goal.objects.create(
            tenant=self.tenant,
            title="Pay off credit card by August",
            description="Snowball method, $200/mo extra.",
            pillar="gravity",
            status=Goal.Status.ACTIVE,
        )
        response = self.client.get(self._url(), {"claim": "paid the credit card $400"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        goal_candidates = [c for c in body["candidates"] if c["kind"] == "goal"]
        self.assertEqual(len(goal_candidates), 1)
        self.assertEqual(goal_candidates[0]["id"], str(goal.id))
        self.assertIn("nbhd_goal_achieve", goal_candidates[0]["update_tools"])

    def test_skips_achieved_goals(self):
        Goal.objects.create(
            tenant=self.tenant,
            title="Pay off credit card",
            status=Goal.Status.ACHIEVED,
        )
        response = self.client.get(self._url(), {"claim": "paid the credit card"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([c for c in body["candidates"] if c["kind"] == "goal"], [])

    # ── tasks ────────────────────────────────────────────────────────

    def test_matches_open_task_by_token(self):
        task = Task.objects.create(
            tenant=self.tenant,
            title="Submit Q1 expense report",
            status=Task.Status.OPEN,
        )
        response = self.client.get(self._url(), {"claim": "submitted my expense report today"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        task_candidates = [c for c in body["candidates"] if c["kind"] == "task"]
        self.assertEqual(len(task_candidates), 1)
        self.assertEqual(task_candidates[0]["id"], str(task.id))
        self.assertIn("nbhd_task_complete", task_candidates[0]["update_tools"])

    def test_skips_done_tasks(self):
        Task.objects.create(
            tenant=self.tenant,
            title="Submit expense report",
            status=Task.Status.DONE,
        )
        response = self.client.get(self._url(), {"claim": "submitted expense report"}, **self._headers())
        body = response.json()
        self.assertEqual([c for c in body["candidates"] if c["kind"] == "task"], [])

    # ── finance ──────────────────────────────────────────────────────

    def test_finance_keyword_triggers_account_candidates(self):
        from apps.finance.models import FinanceAccount

        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type=FinanceAccount.AccountType.CREDIT_CARD,
            nickname="Chase Sapphire",
            current_balance=Decimal("2220.00"),
            minimum_payment=Decimal("75.00"),
            due_day=14,
        )
        response = self.client.get(self._url(), {"claim": "paid $400 on the card"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["triggered"]["finance"])
        finance_candidates = [c for c in body["candidates"] if c["kind"] == "finance_account"]
        self.assertEqual(len(finance_candidates), 1)
        self.assertEqual(finance_candidates[0]["title"], "Chase Sapphire")
        self.assertIn("nbhd_finance_record_payment", finance_candidates[0]["update_tools"])

    def test_no_finance_keyword_skips_finance_candidates(self):
        from apps.finance.models import FinanceAccount

        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type=FinanceAccount.AccountType.CREDIT_CARD,
            nickname="Chase",
            current_balance=Decimal("1000.00"),
        )
        response = self.client.get(self._url(), {"claim": "had a great morning run"}, **self._headers())
        body = response.json()
        self.assertFalse(body["triggered"]["finance"])
        self.assertEqual([c for c in body["candidates"] if c["kind"] == "finance_account"], [])

    def test_finance_disabled_tenant_skipped(self):
        self.tenant.finance_enabled = False
        self.tenant.save(update_fields=["finance_enabled"])
        from apps.finance.models import FinanceAccount

        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type=FinanceAccount.AccountType.CREDIT_CARD,
            nickname="Card",
            current_balance=Decimal("100.00"),
        )
        response = self.client.get(self._url(), {"claim": "paid the card"}, **self._headers())
        body = response.json()
        self.assertEqual([c for c in body["candidates"] if c["kind"] == "finance_account"], [])

    # ── fuel ─────────────────────────────────────────────────────────

    def test_fuel_keyword_triggers_recent_workouts(self):
        today = _date.today()
        workout = Workout.objects.create(
            tenant=self.tenant,
            date=today,
            category=WorkoutCategory.STRENGTH,
            activity="Push — Chest & Shoulders",
            status=WorkoutStatus.PLANNED,
        )
        response = self.client.get(self._url(), {"claim": "did my push workout today"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["triggered"]["fuel"])
        fuel_candidates = [c for c in body["candidates"] if c["kind"] == "fuel_workout"]
        self.assertEqual(len(fuel_candidates), 1)
        self.assertEqual(fuel_candidates[0]["id"], str(workout.id))
        self.assertIn("nbhd_fuel_update_workout", fuel_candidates[0]["update_tools"])

    def test_weight_keyword_surfaces_latest_body_weight(self):
        BodyWeightLog.objects.create(
            tenant=self.tenant,
            date=_date.today(),
            weight_kg=Decimal("82.50"),
        )
        response = self.client.get(self._url(), {"claim": "weighed in at 180 lbs this morning"}, **self._headers())
        body = response.json()
        weight_candidates = [c for c in body["candidates"] if c["kind"] == "fuel_body_weight"]
        self.assertEqual(len(weight_candidates), 1)
        self.assertEqual(weight_candidates[0]["current_state"]["weight_kg"], "82.50")

    # ── tenant isolation ────────────────────────────────────────────

    def test_other_tenants_goals_not_returned(self):
        Goal.objects.create(
            tenant=self.other_tenant,
            title="Pay off credit card",
            status=Goal.Status.ACTIVE,
        )
        response = self.client.get(self._url(), {"claim": "paid credit card"}, **self._headers())
        body = response.json()
        self.assertEqual([c for c in body["candidates"] if c["kind"] == "goal"], [])

    # ── empty result ────────────────────────────────────────────────

    def test_unmatched_claim_returns_empty_candidates(self):
        Goal.objects.create(
            tenant=self.tenant,
            title="Learn Spanish",
            status=Goal.Status.ACTIVE,
        )
        response = self.client.get(self._url(), {"claim": "had a sandwich for lunch"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["candidates"], [])

    # ── multi-section match ─────────────────────────────────────────

    def test_multi_section_match_orders_by_score(self):
        from apps.finance.models import FinanceAccount

        goal = Goal.objects.create(
            tenant=self.tenant,
            title="Pay off credit card by August",
            description="Snowball method, credit card debt focus.",
            status=Goal.Status.ACTIVE,
        )
        Task.objects.create(
            tenant=self.tenant,
            title="Pay May credit card bill",
            status=Task.Status.OPEN,
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type=FinanceAccount.AccountType.CREDIT_CARD,
            nickname="Chase",
            current_balance=Decimal("1820.00"),
        )
        response = self.client.get(
            self._url(),
            {"claim": "paid $400 on credit card"},
            **self._headers(),
        )
        body = response.json()
        kinds = {c["kind"] for c in body["candidates"]}
        self.assertIn("goal", kinds)
        self.assertIn("task", kinds)
        self.assertIn("finance_account", kinds)
        # Goal should have highest score (matches "credit" + "card" in both title + description).
        self.assertEqual(body["candidates"][0]["kind"], "goal")
        self.assertEqual(body["candidates"][0]["id"], str(goal.id))
