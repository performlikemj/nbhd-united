"""Finance module tests — services, models, runtime views, consumer views, snapshots."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest import TestCase as UnitTestCase

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.tenants.services import create_tenant

from .models import FinanceAccount, FinanceSnapshot, FinanceTransaction, PayoffPlan
from .services import DebtInput, calculate_payoff, compare_strategies


# ═════════════════════════════════════════════════════════════════════
# 1. Payoff Calculation Service (pure math, no DB)
# ═════════════════════════════════════════════════════════════════════


class PayoffCalculationTests(UnitTestCase):
    """Test the core payoff calculation engine."""

    def _make_debts(self):
        return [
            DebtInput(
                nickname="High Rate CC",
                balance=Decimal("5000"),
                interest_rate=Decimal("24.99"),
                minimum_payment=Decimal("100"),
            ),
            DebtInput(
                nickname="Low Rate Loan",
                balance=Decimal("10000"),
                interest_rate=Decimal("5.5"),
                minimum_payment=Decimal("200"),
            ),
            DebtInput(
                nickname="Small Balance CC",
                balance=Decimal("1500"),
                interest_rate=Decimal("18.0"),
                minimum_payment=Decimal("50"),
            ),
        ]

    def test_snowball_smallest_first(self):
        """Snowball should target smallest balance first."""
        debts = self._make_debts()
        result = calculate_payoff(debts, Decimal("600"), "snowball", date(2026, 4, 1))

        self.assertEqual(result.strategy, "snowball")
        self.assertGreater(result.payoff_months, 0)
        self.assertGreater(result.total_interest, Decimal("0"))
        self.assertEqual(len(result.schedule), result.payoff_months)

        first_month = result.schedule[0]
        payments = {a.nickname: a.payment for a in first_month.accounts}
        # Small Balance CC ($1500) is targeted — should get more than minimum
        self.assertGreater(payments["Small Balance CC"], Decimal("50"))

    def test_avalanche_highest_rate_first(self):
        """Avalanche should target highest interest rate first."""
        debts = self._make_debts()
        result = calculate_payoff(debts, Decimal("600"), "avalanche", date(2026, 4, 1))

        self.assertEqual(result.strategy, "avalanche")
        first_month = result.schedule[0]
        payments = {a.nickname: a.payment for a in first_month.accounts}
        # High Rate CC (24.99%) is targeted — should get more than minimum
        self.assertGreater(payments["High Rate CC"], Decimal("100"))

    def test_avalanche_saves_most_interest(self):
        """Avalanche should always have lowest total interest."""
        debts = self._make_debts()
        results = compare_strategies(debts, Decimal("600"), date(2026, 4, 1))
        self.assertLessEqual(
            results["avalanche"].total_interest,
            results["snowball"].total_interest,
        )

    def test_empty_debts(self):
        result = calculate_payoff([], Decimal("500"), "snowball")
        self.assertEqual(result.payoff_months, 0)
        self.assertEqual(result.total_interest, Decimal("0"))
        self.assertEqual(result.schedule, [])

    def test_single_debt_zero_interest(self):
        debts = [
            DebtInput(nickname="Card", balance=Decimal("1000"),
                      interest_rate=Decimal("0"), minimum_payment=Decimal("100")),
        ]
        result = calculate_payoff(debts, Decimal("500"), "snowball", date(2026, 4, 1))
        self.assertEqual(result.payoff_months, 2)
        self.assertEqual(result.total_interest, Decimal("0"))
        self.assertEqual(result.payoff_date, date(2026, 6, 1))

    def test_budget_less_than_minimums(self):
        debts = [
            DebtInput(nickname="A", balance=Decimal("5000"),
                      interest_rate=Decimal("10"), minimum_payment=Decimal("200")),
            DebtInput(nickname="B", balance=Decimal("3000"),
                      interest_rate=Decimal("15"), minimum_payment=Decimal("150")),
        ]
        result = calculate_payoff(debts, Decimal("300"), "avalanche", date(2026, 4, 1))
        self.assertGreater(result.payoff_months, 0)

    def test_compare_strategies_returns_all_three(self):
        results = compare_strategies(self._make_debts(), Decimal("600"))
        self.assertEqual(set(results.keys()), {"snowball", "avalanche", "hybrid"})

    def test_schedule_balance_decreases(self):
        result = calculate_payoff(self._make_debts(), Decimal("600"), "avalanche", date(2026, 4, 1))
        for i in range(1, len(result.schedule)):
            self.assertLessEqual(
                result.schedule[i].total_remaining,
                result.schedule[i - 1].total_remaining,
            )

    def test_payoff_date_is_correct(self):
        start = date(2026, 4, 1)
        result = calculate_payoff(self._make_debts(), Decimal("600"), "snowball", start)
        from dateutil.relativedelta import relativedelta
        self.assertEqual(result.payoff_date, start + relativedelta(months=result.payoff_months))

    def test_final_balance_is_zero(self):
        result = calculate_payoff(self._make_debts(), Decimal("600"), "avalanche", date(2026, 4, 1))
        last_month = result.schedule[-1]
        self.assertEqual(last_month.total_remaining, Decimal("0"))
        for a in last_month.accounts:
            self.assertEqual(a.balance, Decimal("0"))


# ═════════════════════════════════════════════════════════════════════
# 2. Model Property Tests
# ═════════════════════════════════════════════════════════════════════


class FinanceAccountModelTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Model Test", telegram_chat_id=900001)

    def test_is_debt_for_credit_card(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("1000"),
        )
        self.assertTrue(account.is_debt)

    def test_is_debt_false_for_savings(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant, account_type="savings",
            nickname="Savings", current_balance=Decimal("5000"),
        )
        self.assertFalse(account.is_debt)

    def test_payoff_progress_with_original_balance(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("600"),
            original_balance=Decimal("1000"),
        )
        self.assertAlmostEqual(account.payoff_progress, 40.0)

    def test_payoff_progress_fully_paid(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("0"),
            original_balance=Decimal("1000"),
        )
        self.assertAlmostEqual(account.payoff_progress, 100.0)

    def test_payoff_progress_none_without_original(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("1000"),
        )
        self.assertIsNone(account.payoff_progress)

    def test_payoff_progress_none_for_savings(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant, account_type="savings",
            nickname="Sav", current_balance=Decimal("5000"),
            original_balance=Decimal("1000"),
        )
        self.assertIsNone(account.payoff_progress)

    def test_soft_delete(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("1000"),
        )
        account.is_active = False
        account.save()
        active = FinanceAccount.objects.filter(tenant=self.tenant, is_active=True)
        self.assertEqual(active.count(), 0)


class FinanceSnapshotModelTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Snap Test", telegram_chat_id=900002)

    def test_unique_together_tenant_date(self):
        FinanceSnapshot.objects.create(
            tenant=self.tenant, date=date(2026, 4, 1),
            total_debt=Decimal("5000"), total_savings=Decimal("0"),
        )
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            FinanceSnapshot.objects.create(
                tenant=self.tenant, date=date(2026, 4, 1),
                total_debt=Decimal("4500"), total_savings=Decimal("0"),
            )


# ═════════════════════════════════════════════════════════════════════
# 3. Runtime View Tests (OpenClaw plugin → Django)
# ═════════════════════════════════════════════════════════════════════


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class RuntimeFinanceViewTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Runtime Finance", telegram_chat_id=900010)
        self.other_tenant = create_tenant(display_name="Other", telegram_chat_id=900011)

    def _headers(self, tenant_id=None, key="test-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def _url(self, suffix):
        return f"/api/v1/finance/runtime/{self.tenant.id}{suffix}"

    # ── Auth ────────────────────────────────────────────────────────

    def test_accounts_requires_auth(self):
        response = self.client.get(self._url("/accounts/"))
        self.assertEqual(response.status_code, 401)

    def test_accounts_rejects_wrong_key(self):
        response = self.client.get(self._url("/accounts/"), **self._headers(key="wrong"))
        self.assertEqual(response.status_code, 401)

    def test_accounts_rejects_tenant_scope_mismatch(self):
        response = self.client.get(
            self._url("/accounts/"),
            **self._headers(tenant_id=str(self.other_tenant.id)),
        )
        self.assertEqual(response.status_code, 401)

    # ── Accounts CRUD ───────────────────────────────────────────────

    def test_create_account(self):
        response = self.client.post(
            self._url("/accounts/"),
            data={
                "nickname": "Chase Card",
                "account_type": "credit_card",
                "current_balance": 4200,
                "interest_rate": 22.9,
                "minimum_payment": 120,
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["nickname"], "Chase Card")
        self.assertTrue(body["created"])
        # original_balance should be auto-set
        account = FinanceAccount.objects.get(id=body["id"])
        self.assertEqual(account.original_balance, Decimal("4200"))

    def test_upsert_account_by_nickname(self):
        """Second create with same nickname should update, not duplicate."""
        self.client.post(
            self._url("/accounts/"),
            data={"nickname": "Chase Card", "account_type": "credit_card", "current_balance": 4200},
            content_type="application/json",
            **self._headers(),
        )
        response = self.client.post(
            self._url("/accounts/"),
            data={"nickname": "chase card", "account_type": "credit_card", "current_balance": 3800},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["created"])
        self.assertEqual(
            FinanceAccount.objects.filter(tenant=self.tenant, is_active=True).count(), 1,
        )
        self.assertEqual(
            FinanceAccount.objects.first().current_balance, Decimal("3800"),
        )

    def test_create_account_requires_nickname(self):
        response = self.client.post(
            self._url("/accounts/"),
            data={"account_type": "credit_card", "current_balance": 1000},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)

    def test_create_account_requires_balance(self):
        response = self.client.post(
            self._url("/accounts/"),
            data={"nickname": "Card", "account_type": "credit_card"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_account_type_defaults_to_other_debt(self):
        response = self.client.post(
            self._url("/accounts/"),
            data={"nickname": "Stuff", "account_type": "magic_beans", "current_balance": 500},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            FinanceAccount.objects.first().account_type, "other_debt",
        )

    def test_list_accounts(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC1", current_balance=Decimal("1000"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="savings",
            nickname="Sav1", current_balance=Decimal("5000"),
        )
        response = self.client.get(self._url("/accounts/"), **self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["accounts"]), 2)

    # ── Tenant Isolation ────────────────────────────────────────────

    def test_accounts_isolated_by_tenant(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="My Card", current_balance=Decimal("1000"),
        )
        FinanceAccount.objects.create(
            tenant=self.other_tenant, account_type="credit_card",
            nickname="Their Card", current_balance=Decimal("2000"),
        )
        response = self.client.get(self._url("/accounts/"), **self._headers())
        accounts = response.json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["nickname"], "My Card")

    # ── Transactions ────────────────────────────────────────────────

    def test_record_payment_updates_balance(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="Chase Card", current_balance=Decimal("4200"),
        )
        response = self.client.post(
            self._url("/transactions/"),
            data={"account_nickname": "Chase Card", "amount": 500},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["new_balance"], "3700.00")
        self.assertEqual(body["transaction_type"], "payment")
        self.assertEqual(FinanceTransaction.objects.count(), 1)

    def test_record_charge_increases_balance(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("1000"),
        )
        response = self.client.post(
            self._url("/transactions/"),
            data={"account_nickname": "CC", "amount": 200, "transaction_type": "charge"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["new_balance"], "1200.00")

    def test_payment_cannot_go_below_zero(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("100"),
        )
        response = self.client.post(
            self._url("/transactions/"),
            data={"account_nickname": "CC", "amount": 500},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["new_balance"], "0.00")

    def test_transaction_fuzzy_nickname_match(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="Chase Sapphire Preferred", current_balance=Decimal("3000"),
        )
        response = self.client.post(
            self._url("/transactions/"),
            data={"account_nickname": "chase sapphire", "amount": 100},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["account_nickname"], "Chase Sapphire Preferred")

    def test_transaction_unknown_account_returns_404(self):
        response = self.client.post(
            self._url("/transactions/"),
            data={"account_nickname": "Nonexistent", "amount": 100},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 404)

    # ── Balance Update ──────────────────────────────────────────────

    def test_update_balance(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("4200"),
        )
        response = self.client.post(
            self._url("/balance/"),
            data={"account_nickname": "CC", "new_balance": 3800},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["old_balance"], "4200.00")
        self.assertEqual(body["new_balance"], "3800.00")

    def test_update_balance_unknown_account(self):
        response = self.client.post(
            self._url("/balance/"),
            data={"account_nickname": "Ghost", "new_balance": 100},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 404)

    # ── Payoff Calculation ──────────────────────────────────────────

    def test_payoff_calculate_all_strategies(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("5000"),
            interest_rate=Decimal("20"), minimum_payment=Decimal("100"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="auto_loan",
            nickname="Car", current_balance=Decimal("8000"),
            interest_rate=Decimal("6"), minimum_payment=Decimal("200"),
        )
        response = self.client.post(
            self._url("/payoff/calculate/"),
            data={"monthly_budget": 600},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(set(results.keys()), {"snowball", "avalanche", "hybrid"})
        for strategy in results.values():
            self.assertGreater(strategy["payoff_months"], 0)
            self.assertIn("schedule", strategy)

    def test_payoff_calculate_single_strategy(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("3000"),
            interest_rate=Decimal("18"), minimum_payment=Decimal("80"),
        )
        response = self.client.post(
            self._url("/payoff/calculate/"),
            data={"monthly_budget": 500, "strategy": "avalanche"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(list(results.keys()), ["avalanche"])

    def test_payoff_save_creates_plan_and_deactivates_old(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("3000"),
            interest_rate=Decimal("18"), minimum_payment=Decimal("80"),
        )
        # Create initial plan
        old_plan = PayoffPlan.objects.create(
            tenant=self.tenant, strategy="snowball",
            monthly_budget=Decimal("500"), total_debt=Decimal("3000"),
            total_interest=Decimal("200"), payoff_months=7,
            payoff_date=date(2026, 11, 1), is_active=True,
        )
        # Save a new plan
        response = self.client.post(
            self._url("/payoff/calculate/"),
            data={"monthly_budget": 500, "strategy": "avalanche", "save": True},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        old_plan.refresh_from_db()
        self.assertFalse(old_plan.is_active)
        new_plan = PayoffPlan.objects.filter(tenant=self.tenant, is_active=True).first()
        self.assertIsNotNone(new_plan)
        self.assertEqual(new_plan.strategy, "avalanche")

    def test_payoff_no_debts_returns_empty(self):
        # Only a savings account, no debts
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="savings",
            nickname="Sav", current_balance=Decimal("5000"),
        )
        response = self.client.post(
            self._url("/payoff/calculate/"),
            data={"monthly_budget": 500},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], {})

    def test_payoff_requires_monthly_budget(self):
        response = self.client.post(
            self._url("/payoff/calculate/"),
            data={},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)

    # ── Summary ─────────────────────────────────────────────────────

    def test_summary_aggregates_correctly(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC1", current_balance=Decimal("3000"),
            interest_rate=Decimal("20"), minimum_payment=Decimal("80"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="auto_loan",
            nickname="Car", current_balance=Decimal("8000"),
            interest_rate=Decimal("6"), minimum_payment=Decimal("200"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="savings",
            nickname="Emergency", current_balance=Decimal("2500"),
        )
        PayoffPlan.objects.create(
            tenant=self.tenant, strategy="avalanche",
            monthly_budget=Decimal("500"), total_debt=Decimal("11000"),
            total_interest=Decimal("1500"), payoff_months=24,
            payoff_date=date(2028, 4, 1), is_active=True,
        )

        response = self.client.get(self._url("/summary/"), **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_debt"], "11000.00")
        self.assertEqual(body["total_savings"], "2500.00")
        self.assertEqual(body["total_minimum_payments"], "280.00")
        self.assertEqual(body["debt_account_count"], 2)
        self.assertEqual(body["savings_account_count"], 1)
        self.assertEqual(len(body["accounts"]), 3)
        self.assertIsNotNone(body["active_plan"])
        self.assertEqual(body["active_plan"]["strategy"], "avalanche")

    def test_summary_no_active_plan(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("1000"),
        )
        response = self.client.get(self._url("/summary/"), **self._headers())
        body = response.json()
        self.assertIsNone(body["active_plan"])

    def test_summary_isolated_by_tenant(self):
        FinanceAccount.objects.create(
            tenant=self.other_tenant, account_type="credit_card",
            nickname="Their Card", current_balance=Decimal("9999"),
        )
        response = self.client.get(self._url("/summary/"), **self._headers())
        body = response.json()
        self.assertEqual(body["total_debt"], "0")
        self.assertEqual(len(body["accounts"]), 0)


# ═════════════════════════════════════════════════════════════════════
# 4. Consumer API Tests (frontend, JWT auth)
# ═════════════════════════════════════════════════════════════════════


class ConsumerFinanceViewTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Consumer", telegram_chat_id=900020)
        self.other_tenant = create_tenant(display_name="Other Consumer", telegram_chat_id=900021)
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def test_dashboard_requires_auth(self):
        unauthed = APIClient()
        response = unauthed.get("/api/v1/finance/dashboard/")
        self.assertEqual(response.status_code, 401)

    def test_dashboard_empty(self):
        response = self.client.get("/api/v1/finance/dashboard/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_debt"], "0.00")
        self.assertEqual(body["total_savings"], "0.00")
        self.assertEqual(body["accounts"], [])
        self.assertIsNone(body["active_plan"])

    def test_dashboard_aggregation(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("5000"),
            original_balance=Decimal("8000"),
            interest_rate=Decimal("20"), minimum_payment=Decimal("120"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="savings",
            nickname="Sav", current_balance=Decimal("2000"),
        )
        response = self.client.get("/api/v1/finance/dashboard/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_debt"], "5000.00")
        self.assertEqual(body["total_savings"], "2000.00")
        self.assertEqual(body["debt_account_count"], 1)
        self.assertEqual(body["savings_account_count"], 1)
        self.assertEqual(len(body["accounts"]), 2)
        # Payoff progress should be included
        cc = next(a for a in body["accounts"] if a["nickname"] == "CC")
        self.assertAlmostEqual(cc["payoff_progress"], 37.5)

    def test_dashboard_excludes_inactive_accounts(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="Active", current_balance=Decimal("1000"), is_active=True,
        )
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="Deleted", current_balance=Decimal("2000"), is_active=False,
        )
        response = self.client.get("/api/v1/finance/dashboard/")
        body = response.json()
        self.assertEqual(len(body["accounts"]), 1)
        self.assertEqual(body["accounts"][0]["nickname"], "Active")

    def test_dashboard_isolated_by_tenant(self):
        FinanceAccount.objects.create(
            tenant=self.other_tenant, account_type="credit_card",
            nickname="Not Mine", current_balance=Decimal("9999"),
        )
        response = self.client.get("/api/v1/finance/dashboard/")
        self.assertEqual(len(response.json()["accounts"]), 0)

    def test_accounts_list(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("3000"),
        )
        response = self.client.get("/api/v1/finance/accounts/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_account_create(self):
        response = self.client.post(
            "/api/v1/finance/accounts/",
            data={
                "nickname": "New Card",
                "account_type": "credit_card",
                "current_balance": "2500.00",
                "interest_rate": "19.99",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["nickname"], "New Card")
        self.assertEqual(FinanceAccount.objects.count(), 1)

    def test_account_patch(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("5000"),
        )
        response = self.client.patch(
            f"/api/v1/finance/accounts/{account.id}/",
            data={"current_balance": "4500.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal("4500.00"))

    def test_account_delete_soft_deletes(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("1000"),
        )
        response = self.client.delete(f"/api/v1/finance/accounts/{account.id}/")
        self.assertEqual(response.status_code, 204)
        account.refresh_from_db()
        self.assertFalse(account.is_active)

    def test_account_detail_404_for_other_tenant(self):
        account = FinanceAccount.objects.create(
            tenant=self.other_tenant, account_type="credit_card",
            nickname="Not Mine", current_balance=Decimal("9999"),
        )
        response = self.client.patch(
            f"/api/v1/finance/accounts/{account.id}/",
            data={"current_balance": "0"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_transactions_list(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("3000"),
        )
        FinanceTransaction.objects.create(
            tenant=self.tenant, account=account,
            transaction_type="payment", amount=Decimal("200"),
            date=date(2026, 3, 15),
        )
        response = self.client.get("/api/v1/finance/transactions/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_payoff_plans_list(self):
        PayoffPlan.objects.create(
            tenant=self.tenant, strategy="avalanche",
            monthly_budget=Decimal("500"), total_debt=Decimal("10000"),
            total_interest=Decimal("1200"), payoff_months=22,
            payoff_date=date(2028, 1, 1), is_active=True,
        )
        response = self.client.get("/api/v1/finance/payoff-plans/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_snapshots_list(self):
        FinanceSnapshot.objects.create(
            tenant=self.tenant, date=date(2026, 3, 1),
            total_debt=Decimal("10000"), total_savings=Decimal("2000"),
        )
        response = self.client.get("/api/v1/finance/snapshots/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)


# ═════════════════════════════════════════════════════════════════════
# 5. Snapshot Cron Tests
# ═════════════════════════════════════════════════════════════════════


class SnapshotServiceTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Snap", telegram_chat_id=900030)
        self.tenant.finance_enabled = True
        self.tenant.status = "active"
        self.tenant.save()

    def test_creates_snapshot_for_active_finance_tenant(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("5000"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="savings",
            nickname="Sav", current_balance=Decimal("2000"),
        )

        from .snapshot import create_monthly_snapshots
        count = create_monthly_snapshots(date(2026, 4, 1))

        self.assertEqual(count, 1)
        snap = FinanceSnapshot.objects.get(tenant=self.tenant, date=date(2026, 4, 1))
        self.assertEqual(snap.total_debt, Decimal("5000"))
        self.assertEqual(snap.total_savings, Decimal("2000"))
        self.assertEqual(len(snap.accounts_json), 2)

    def test_idempotent_no_duplicate_snapshots(self):
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("3000"),
        )
        from .snapshot import create_monthly_snapshots
        create_monthly_snapshots(date(2026, 4, 1))
        count = create_monthly_snapshots(date(2026, 4, 1))
        self.assertEqual(count, 1)  # still 1 — skipped the duplicate
        self.assertEqual(
            FinanceSnapshot.objects.filter(tenant=self.tenant).count(), 1,
        )

    def test_skips_tenants_without_accounts(self):
        from .snapshot import create_monthly_snapshots
        count = create_monthly_snapshots(date(2026, 4, 1))
        # Tenant has finance_enabled but no accounts — should skip
        self.assertEqual(count, 1)  # create_monthly_snapshots counts tenants attempted
        self.assertEqual(FinanceSnapshot.objects.count(), 0)

    def test_skips_tenants_with_finance_disabled(self):
        self.tenant.finance_enabled = False
        self.tenant.save()
        FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("1000"),
        )
        from .snapshot import create_monthly_snapshots
        count = create_monthly_snapshots(date(2026, 4, 1))
        self.assertEqual(count, 0)
        self.assertEqual(FinanceSnapshot.objects.count(), 0)

    def test_aggregates_previous_month_payments(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant, account_type="credit_card",
            nickname="CC", current_balance=Decimal("4000"),
        )
        FinanceTransaction.objects.create(
            tenant=self.tenant, account=account,
            transaction_type="payment", amount=Decimal("300"),
            date=date(2026, 3, 10),
        )
        FinanceTransaction.objects.create(
            tenant=self.tenant, account=account,
            transaction_type="payment", amount=Decimal("200"),
            date=date(2026, 3, 25),
        )
        # This one is from February — should NOT be included
        FinanceTransaction.objects.create(
            tenant=self.tenant, account=account,
            transaction_type="payment", amount=Decimal("100"),
            date=date(2026, 2, 15),
        )

        from .snapshot import create_monthly_snapshots
        create_monthly_snapshots(date(2026, 4, 1))
        snap = FinanceSnapshot.objects.get(tenant=self.tenant)
        self.assertEqual(snap.total_payments_this_month, Decimal("500"))
