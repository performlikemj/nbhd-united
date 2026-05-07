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
            DebtInput(
                nickname="Card", balance=Decimal("1000"), interest_rate=Decimal("0"), minimum_payment=Decimal("100")
            ),
        ]
        result = calculate_payoff(debts, Decimal("500"), "snowball", date(2026, 4, 1))
        self.assertEqual(result.payoff_months, 2)
        self.assertEqual(result.total_interest, Decimal("0"))
        self.assertEqual(result.payoff_date, date(2026, 6, 1))

    def test_budget_less_than_minimums(self):
        debts = [
            DebtInput(
                nickname="A", balance=Decimal("5000"), interest_rate=Decimal("10"), minimum_payment=Decimal("200")
            ),
            DebtInput(
                nickname="B", balance=Decimal("3000"), interest_rate=Decimal("15"), minimum_payment=Decimal("150")
            ),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("1000"),
        )
        self.assertTrue(account.is_debt)

    def test_is_debt_false_for_savings(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="savings",
            nickname="Savings",
            current_balance=Decimal("5000"),
        )
        self.assertFalse(account.is_debt)

    def test_payoff_progress_with_original_balance(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("600"),
            original_balance=Decimal("1000"),
        )
        self.assertAlmostEqual(account.payoff_progress, 40.0)

    def test_payoff_progress_fully_paid(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("0"),
            original_balance=Decimal("1000"),
        )
        self.assertAlmostEqual(account.payoff_progress, 100.0)

    def test_payoff_progress_none_without_original(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("1000"),
        )
        self.assertIsNone(account.payoff_progress)

    def test_payoff_progress_none_for_savings(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="savings",
            nickname="Sav",
            current_balance=Decimal("5000"),
            original_balance=Decimal("1000"),
        )
        self.assertIsNone(account.payoff_progress)

    def test_soft_delete(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("1000"),
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
            tenant=self.tenant,
            date=date(2026, 4, 1),
            total_debt=Decimal("5000"),
            total_savings=Decimal("0"),
        )
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            FinanceSnapshot.objects.create(
                tenant=self.tenant,
                date=date(2026, 4, 1),
                total_debt=Decimal("4500"),
                total_savings=Decimal("0"),
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
            FinanceAccount.objects.filter(tenant=self.tenant, is_active=True).count(),
            1,
        )
        self.assertEqual(
            FinanceAccount.objects.first().current_balance,
            Decimal("3800"),
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
            FinanceAccount.objects.first().account_type,
            "other_debt",
        )

    def test_list_accounts(self):
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC1",
            current_balance=Decimal("1000"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="savings",
            nickname="Sav1",
            current_balance=Decimal("5000"),
        )
        response = self.client.get(self._url("/accounts/"), **self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["accounts"]), 2)

    # ── Tenant Isolation ────────────────────────────────────────────

    def test_accounts_isolated_by_tenant(self):
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="My Card",
            current_balance=Decimal("1000"),
        )
        FinanceAccount.objects.create(
            tenant=self.other_tenant,
            account_type="credit_card",
            nickname="Their Card",
            current_balance=Decimal("2000"),
        )
        response = self.client.get(self._url("/accounts/"), **self._headers())
        accounts = response.json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["nickname"], "My Card")

    # ── Transactions ────────────────────────────────────────────────

    def test_record_payment_updates_balance(self):
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="Chase Card",
            current_balance=Decimal("4200"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("1000"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("100"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="Chase Sapphire Preferred",
            current_balance=Decimal("3000"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("4200"),
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

    # ── Archive / Unarchive ─────────────────────────────────────────

    def test_archive_account_by_nickname(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="student_loan",
            nickname="Federal Student Loans",
            current_balance=Decimal("39706.91"),
        )
        response = self.client.post(
            self._url("/accounts/archive/"),
            data={"account_nickname": "Federal Student Loans"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["archived"])
        self.assertEqual(body["account_nickname"], "Federal Student Loans")
        self.assertEqual(body["previous_balance"], "39706.91")
        account.refresh_from_db()
        self.assertFalse(account.is_active)

    def test_archive_account_fuzzy_match(self):
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="student_loan",
            nickname="Federal Student Loans",
            current_balance=Decimal("1000"),
        )
        response = self.client.post(
            self._url("/accounts/archive/"),
            data={"account_nickname": "federal student"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account_nickname"], "Federal Student Loans")

    def test_archive_account_not_found(self):
        response = self.client.post(
            self._url("/accounts/archive/"),
            data={"account_nickname": "Ghost"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 404)

    def test_archive_account_tenant_isolation(self):
        FinanceAccount.objects.create(
            tenant=self.other_tenant,
            account_type="credit_card",
            nickname="Their Card",
            current_balance=Decimal("500"),
        )
        response = self.client.post(
            self._url("/accounts/archive/"),
            data={"account_nickname": "Their Card"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 404)

    def test_archive_account_requires_nickname(self):
        response = self.client.post(
            self._url("/accounts/archive/"),
            data={},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)

    def test_archive_excludes_from_payoff_calculation(self):
        """Archived debts should not appear in payoff calculations."""
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="Active CC",
            current_balance=Decimal("2000"),
            interest_rate=Decimal("20"),
            minimum_payment=Decimal("50"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="student_loan",
            nickname="Old Loan",
            current_balance=Decimal("10000"),
            interest_rate=Decimal("5"),
            minimum_payment=Decimal("100"),
            is_active=False,
        )
        response = self.client.post(
            self._url("/payoff/calculate/"),
            data={"monthly_budget": 400, "strategy": "avalanche"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        schedule = response.json()["results"]["avalanche"]["schedule"]
        first_month_accounts = {a["nickname"] for a in schedule[0]["accounts"]}
        self.assertIn("Active CC", first_month_accounts)
        self.assertNotIn("Old Loan", first_month_accounts)

    def test_unarchive_account(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("1234.56"),
            is_active=False,
        )
        response = self.client.post(
            self._url("/accounts/unarchive/"),
            data={"account_nickname": "CC"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["unarchived"])
        self.assertEqual(body["current_balance"], "1234.56")
        account.refresh_from_db()
        self.assertTrue(account.is_active)

    def test_unarchive_name_collision(self):
        """Cannot unarchive if an active account already has that nickname."""
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="Chase Card",
            current_balance=Decimal("3000"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="chase card",
            current_balance=Decimal("500"),
            is_active=False,
        )
        response = self.client.post(
            self._url("/accounts/unarchive/"),
            data={"account_nickname": "chase card"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "name_collision")

    def test_unarchive_ignores_active_accounts(self):
        """Unarchive scoped to archived rows — cannot target an active one."""
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="Active",
            current_balance=Decimal("100"),
        )
        response = self.client.post(
            self._url("/accounts/unarchive/"),
            data={"account_nickname": "Active"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 404)

    def test_list_accounts_archived_only(self):
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="Active CC",
            current_balance=Decimal("1000"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="student_loan",
            nickname="Old Loan",
            current_balance=Decimal("5000"),
            is_active=False,
        )
        response = self.client.get(
            self._url("/accounts/") + "?archived=true",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        accounts = response.json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["nickname"], "Old Loan")
        self.assertFalse(accounts[0]["is_active"])

    def test_list_accounts_archived_all(self):
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="Active",
            current_balance=Decimal("100"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="Gone",
            current_balance=Decimal("200"),
            is_active=False,
        )
        response = self.client.get(
            self._url("/accounts/") + "?archived=all",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["accounts"]), 2)

    # ── Payoff Calculation ──────────────────────────────────────────

    def test_payoff_calculate_all_strategies(self):
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("5000"),
            interest_rate=Decimal("20"),
            minimum_payment=Decimal("100"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="auto_loan",
            nickname="Car",
            current_balance=Decimal("8000"),
            interest_rate=Decimal("6"),
            minimum_payment=Decimal("200"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("3000"),
            interest_rate=Decimal("18"),
            minimum_payment=Decimal("80"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("3000"),
            interest_rate=Decimal("18"),
            minimum_payment=Decimal("80"),
        )
        # Create initial plan
        old_plan = PayoffPlan.objects.create(
            tenant=self.tenant,
            strategy="snowball",
            monthly_budget=Decimal("500"),
            total_debt=Decimal("3000"),
            total_interest=Decimal("200"),
            payoff_months=7,
            payoff_date=date(2026, 11, 1),
            is_active=True,
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
            tenant=self.tenant,
            account_type="savings",
            nickname="Sav",
            current_balance=Decimal("5000"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC1",
            current_balance=Decimal("3000"),
            interest_rate=Decimal("20"),
            minimum_payment=Decimal("80"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="auto_loan",
            nickname="Car",
            current_balance=Decimal("8000"),
            interest_rate=Decimal("6"),
            minimum_payment=Decimal("200"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="savings",
            nickname="Emergency",
            current_balance=Decimal("2500"),
        )
        PayoffPlan.objects.create(
            tenant=self.tenant,
            strategy="avalanche",
            monthly_budget=Decimal("500"),
            total_debt=Decimal("11000"),
            total_interest=Decimal("1500"),
            payoff_months=24,
            payoff_date=date(2028, 4, 1),
            is_active=True,
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("1000"),
        )
        response = self.client.get(self._url("/summary/"), **self._headers())
        body = response.json()
        self.assertIsNone(body["active_plan"])

    def test_summary_isolated_by_tenant(self):
        FinanceAccount.objects.create(
            tenant=self.other_tenant,
            account_type="credit_card",
            nickname="Their Card",
            current_balance=Decimal("9999"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("5000"),
            original_balance=Decimal("8000"),
            interest_rate=Decimal("20"),
            minimum_payment=Decimal("120"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="savings",
            nickname="Sav",
            current_balance=Decimal("2000"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="Active",
            current_balance=Decimal("1000"),
            is_active=True,
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="Deleted",
            current_balance=Decimal("2000"),
            is_active=False,
        )
        response = self.client.get("/api/v1/finance/dashboard/")
        body = response.json()
        self.assertEqual(len(body["accounts"]), 1)
        self.assertEqual(body["accounts"][0]["nickname"], "Active")

    def test_dashboard_isolated_by_tenant(self):
        FinanceAccount.objects.create(
            tenant=self.other_tenant,
            account_type="credit_card",
            nickname="Not Mine",
            current_balance=Decimal("9999"),
        )
        response = self.client.get("/api/v1/finance/dashboard/")
        self.assertEqual(len(response.json()["accounts"]), 0)

    def test_accounts_list(self):
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("3000"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("5000"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("1000"),
        )
        response = self.client.delete(f"/api/v1/finance/accounts/{account.id}/")
        self.assertEqual(response.status_code, 204)
        account.refresh_from_db()
        self.assertFalse(account.is_active)

    def test_accounts_list_archived_query_param(self):
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="Active",
            current_balance=Decimal("500"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="student_loan",
            nickname="Archived",
            current_balance=Decimal("2000"),
            is_active=False,
        )
        active_response = self.client.get("/api/v1/finance/accounts/")
        self.assertEqual(len(active_response.json()), 1)
        self.assertEqual(active_response.json()[0]["nickname"], "Active")

        archived_response = self.client.get("/api/v1/finance/accounts/?archived=true")
        self.assertEqual(archived_response.status_code, 200)
        body = archived_response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["nickname"], "Archived")
        self.assertFalse(body[0]["is_active"])

    def test_account_patch_unarchive(self):
        """PATCH {is_active: true} should restore an archived account."""
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("1000"),
            is_active=False,
        )
        response = self.client.patch(
            f"/api/v1/finance/accounts/{account.id}/",
            data={"is_active": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        account.refresh_from_db()
        self.assertTrue(account.is_active)

    def test_account_detail_404_for_other_tenant(self):
        account = FinanceAccount.objects.create(
            tenant=self.other_tenant,
            account_type="credit_card",
            nickname="Not Mine",
            current_balance=Decimal("9999"),
        )
        response = self.client.patch(
            f"/api/v1/finance/accounts/{account.id}/",
            data={"current_balance": "0"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_transactions_list(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("3000"),
        )
        FinanceTransaction.objects.create(
            tenant=self.tenant,
            account=account,
            transaction_type="payment",
            amount=Decimal("200"),
            date=date(2026, 3, 15),
        )
        response = self.client.get("/api/v1/finance/transactions/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_payoff_plans_list(self):
        PayoffPlan.objects.create(
            tenant=self.tenant,
            strategy="avalanche",
            monthly_budget=Decimal("500"),
            total_debt=Decimal("10000"),
            total_interest=Decimal("1200"),
            payoff_months=22,
            payoff_date=date(2028, 1, 1),
            is_active=True,
        )
        response = self.client.get("/api/v1/finance/payoff-plans/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_snapshots_list(self):
        FinanceSnapshot.objects.create(
            tenant=self.tenant,
            date=date(2026, 3, 1),
            total_debt=Decimal("10000"),
            total_savings=Decimal("2000"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("5000"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="savings",
            nickname="Sav",
            current_balance=Decimal("2000"),
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("3000"),
        )
        from .snapshot import create_monthly_snapshots

        create_monthly_snapshots(date(2026, 4, 1))
        count = create_monthly_snapshots(date(2026, 4, 1))
        self.assertEqual(count, 1)  # still 1 — skipped the duplicate
        self.assertEqual(
            FinanceSnapshot.objects.filter(tenant=self.tenant).count(),
            1,
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
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("1000"),
        )
        from .snapshot import create_monthly_snapshots

        count = create_monthly_snapshots(date(2026, 4, 1))
        self.assertEqual(count, 0)
        self.assertEqual(FinanceSnapshot.objects.count(), 0)

    def test_aggregates_previous_month_payments(self):
        account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="credit_card",
            nickname="CC",
            current_balance=Decimal("4000"),
        )
        FinanceTransaction.objects.create(
            tenant=self.tenant,
            account=account,
            transaction_type="payment",
            amount=Decimal("300"),
            date=date(2026, 3, 10),
        )
        FinanceTransaction.objects.create(
            tenant=self.tenant,
            account=account,
            transaction_type="payment",
            amount=Decimal("200"),
            date=date(2026, 3, 25),
        )
        # This one is from February — should NOT be included
        FinanceTransaction.objects.create(
            tenant=self.tenant,
            account=account,
            transaction_type="payment",
            amount=Decimal("100"),
            date=date(2026, 2, 15),
        )

        from .snapshot import create_monthly_snapshots

        create_monthly_snapshots(date(2026, 4, 1))
        snap = FinanceSnapshot.objects.get(tenant=self.tenant)
        self.assertEqual(snap.total_payments_this_month, Decimal("500"))


# ═════════════════════════════════════════════════════════════════════
# 7. Phase 1 — Gravity proactive parity (welcome cron + weekly check-in)
# ═════════════════════════════════════════════════════════════════════


class FinanceSettingsViewTests(TestCase):
    """Toggling finance_enabled schedules a welcome cron via QStash."""

    def setUp(self):
        from unittest.mock import patch as _patch

        self.tenant = create_tenant(display_name="GravityToggle", telegram_chat_id=900200)
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        self._patch = _patch
        # Tenant defaults to finance_enabled=False — flip it explicitly when needed.
        self.tenant.finance_enabled = False
        self.tenant.save(update_fields=["finance_enabled"])

    def test_first_enable_schedules_welcome(self):
        with self._patch("apps.cron.publish.publish_task") as mock_publish:
            response = self.client.patch(
                "/api/v1/finance/settings/",
                {"finance_enabled": True},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["finance_enabled"])
        # Welcome scheduling enqueued via QStash (delayed 90s).
        mock_publish.assert_called_once_with(
            "schedule_finance_welcome",
            str(self.tenant.id),
            delay_seconds=90,
        )

    def test_re_enable_does_not_reschedule_welcome(self):
        # Already enabled — toggling on a second time shouldn't fire QStash.
        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["finance_enabled"])

        with self._patch("apps.cron.publish.publish_task") as mock_publish:
            response = self.client.patch(
                "/api/v1/finance/settings/",
                {"finance_enabled": True},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        mock_publish.assert_not_called()

    def test_disable_does_not_schedule_welcome(self):
        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["finance_enabled"])

        with self._patch("apps.cron.publish.publish_task") as mock_publish:
            response = self.client.patch(
                "/api/v1/finance/settings/",
                {"finance_enabled": False},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        mock_publish.assert_not_called()


class GravityWeeklyCheckinSeedJobTests(TestCase):
    """The weekly cron is gated on tenant.finance_enabled at seed-time."""

    def setUp(self):
        self.tenant = create_tenant(display_name="GravityCron", telegram_chat_id=900201)

    def test_absent_when_finance_disabled(self):
        from apps.orchestrator.config_generator import build_cron_seed_jobs

        self.tenant.finance_enabled = False
        self.tenant.save(update_fields=["finance_enabled"])
        jobs = build_cron_seed_jobs(self.tenant)
        names = {j["name"] for j in jobs}
        self.assertNotIn("Gravity Weekly Check-in", names)

    def test_present_when_finance_enabled(self):
        from apps.orchestrator.config_generator import build_cron_seed_jobs

        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["finance_enabled"])
        jobs = build_cron_seed_jobs(self.tenant)
        weekly = next((j for j in jobs if j["name"] == "Gravity Weekly Check-in"), None)
        self.assertIsNotNone(weekly)
        # Sunday at 19:00 in user's timezone.
        self.assertEqual(weekly["schedule"]["expr"], "0 19 * * 0")
        # Foreground (Phase 2 sync) so summary lands in main session.
        self.assertIn("FINAL STEP", weekly["payload"]["message"])
        # Prompt mentions the Gravity terminology and pulls from USER.md.
        self.assertIn("Gravity", weekly["payload"]["message"])
        self.assertIn("USER.md", weekly["payload"]["message"])


class FinanceWelcomePromptTests(UnitTestCase):
    """Sanity-check the static welcome prompt content (no DB)."""

    def test_welcome_prompt_avoids_questions(self):
        from apps.finance.views import _FINANCE_WELCOME_PROMPT

        self.assertIn("Gravity", _FINANCE_WELCOME_PROMPT)
        self.assertIn("nbhd_send_to_user", _FINANCE_WELCOME_PROMPT)
        # Explicit instruction to not ask questions in welcome.
        self.assertIn("Do NOT ask questions", _FINANCE_WELCOME_PROMPT)


class FinanceWelcomeIdempotencyTests(TestCase):
    """``_schedule_finance_welcome`` skips when a welcome cron already exists.

    Re-toggling the feature flag (off→on) while a previous welcome is still
    pending would otherwise create a duplicate cron in the container.
    """

    def setUp(self):
        from unittest.mock import patch as _patch

        self.tenant = create_tenant(display_name="Idem", telegram_chat_id=900250)
        self.tenant.container_fqdn = "oc-test.example.com"
        self.tenant.save(update_fields=["container_fqdn"])
        self._patch = _patch

    def test_skips_when_welcome_already_pending(self):
        from apps.finance.views import _schedule_finance_welcome
        from apps.orchestrator.welcome_scheduler import WelcomeStatus

        with (
            self._patch(
                "apps.cron.gateway_client.cron_exists",
                return_value=True,
            ) as mock_exists,
            self._patch("apps.cron.gateway_client.invoke_gateway_tool") as mock_invoke,
        ):
            status = _schedule_finance_welcome(self.tenant)

        # First call uses require_future_fire=True; if that's True we
        # short-circuit and never make the second (stale-check) call.
        mock_exists.assert_called_once_with(self.tenant, "_finance:welcome", require_future_fire=True)
        self.assertEqual(status, WelcomeStatus.SKIPPED_PENDING)
        # cron.add was NOT called because welcome is already pending.
        for call in mock_invoke.call_args_list:
            self.assertNotEqual(call.args[1] if len(call.args) > 1 else call.kwargs.get("tool"), "cron.add")

    def test_skips_when_welcome_already_delivered(self):
        """welcomes_sent['finance'] set → no re-schedule even if no pending cron."""
        from apps.finance.views import _schedule_finance_welcome
        from apps.orchestrator.welcome_scheduler import WelcomeStatus

        self.tenant.welcomes_sent = {"finance": "2026-05-07T03:00:00+00:00"}
        self.tenant.save(update_fields=["welcomes_sent"])

        with (
            self._patch(
                "apps.cron.gateway_client.cron_exists",
                return_value=False,
            ),
            self._patch("apps.cron.gateway_client.invoke_gateway_tool") as mock_invoke,
        ):
            status = _schedule_finance_welcome(self.tenant)

        self.assertEqual(status, WelcomeStatus.SKIPPED_ALREADY_DELIVERED)
        # No cron.add — already delivered.
        for call in mock_invoke.call_args_list:
            self.assertNotEqual(call.args[1] if len(call.args) > 1 else call.kwargs.get("tool"), "cron.add")

    def test_schedules_when_no_welcome_pending(self):
        from apps.finance.views import _schedule_finance_welcome
        from apps.orchestrator.welcome_scheduler import WelcomeStatus

        with (
            self._patch(
                "apps.cron.gateway_client.cron_exists",
                return_value=False,
            ),
            self._patch("apps.cron.gateway_client.invoke_gateway_tool") as mock_invoke,
        ):
            status = _schedule_finance_welcome(self.tenant)

        self.assertEqual(status, WelcomeStatus.SCHEDULED)
        mock_invoke.assert_called_once()
        args, _kwargs = mock_invoke.call_args
        # invoke_gateway_tool(tenant, "cron.add", {"job": {...}})
        self.assertEqual(args[1], "cron.add")
        self.assertEqual(args[2]["job"]["name"], "_finance:welcome")
        # Prompt has been formatted with the tenant id (no unfilled placeholders).
        message = args[2]["job"]["payload"]["message"]
        self.assertIn(str(self.tenant.id), message)
        self.assertNotIn("{tenant_id}", message)

    def test_replaces_stale_welcome_cron(self):
        """A pending cron whose next-fire is in the past gets removed and re-added.

        Reproduces the canary incident on 2026-05-07: the original Apr 25
        one-shot fired but the agent crashed mid-turn and never self-removed
        the cron. Without freshness-aware idempotency, the stale cron blocks
        re-scheduling for a year (next fire = Apr 25, 2027).
        """
        from apps.finance.views import _schedule_finance_welcome
        from apps.orchestrator.welcome_scheduler import WelcomeStatus

        # Two-phase mock: cron_exists returns False with require_future_fire=True
        # (no pending future cron) but True with require_future_fire=False
        # (a row exists, but its next fire is in the past).
        def fake_exists(_tenant, _name, *, include_disabled=True, require_future_fire=False):
            return not require_future_fire

        with (
            self._patch("apps.cron.gateway_client.cron_exists", side_effect=fake_exists),
            self._patch("apps.cron.gateway_client.invoke_gateway_tool") as mock_invoke,
        ):
            status = _schedule_finance_welcome(self.tenant)

        self.assertEqual(status, WelcomeStatus.REPLACED_STALE)
        tools_called = [call.args[1] for call in mock_invoke.call_args_list if len(call.args) > 1]
        # Both remove (to clear the stale row) and add (fresh schedule).
        self.assertIn("cron.remove", tools_called)
        self.assertIn("cron.add", tools_called)
        # Order matters: remove must precede add so the gateway doesn't
        # see a name collision.
        self.assertLess(tools_called.index("cron.remove"), tools_called.index("cron.add"))

    def test_raises_on_gateway_failure(self):
        """Transport failures bubble up so backfill telemetry is honest.

        Phase 1 swallowed all exceptions inside the helper, which made the
        deploy backfill report ``finance: 1`` even when scheduling silently
        failed. The watchdog and the management command both need real
        exceptions to count "failed" correctly.
        """
        from apps.cron.gateway_client import GatewayError
        from apps.finance.views import _schedule_finance_welcome

        with (
            self._patch("apps.cron.gateway_client.cron_exists", return_value=False),
            self._patch(
                "apps.cron.gateway_client.invoke_gateway_tool",
                side_effect=GatewayError("simulated transport failure"),
            ),self.assertRaises(GatewayError)
        ):
            _schedule_finance_welcome(self.tenant)


class FinanceSettingsViewClearsDeliveryFlagTests(TestCase):
    """Re-enabling Gravity (off→on) clears any prior welcomes_sent['finance']
    so the welcome re-fires for users who want to retest the experience.
    """

    def setUp(self):
        from unittest.mock import patch as _patch

        self.tenant = create_tenant(display_name="Toggle", telegram_chat_id=900260)
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        self._patch = _patch

    def test_off_to_on_clears_delivery_marker(self):
        # Pretend a previous welcome was delivered, then user disabled.
        self.tenant.finance_enabled = False
        self.tenant.welcomes_sent = {"finance": "2026-05-07T03:00:00+00:00"}
        self.tenant.save(update_fields=["finance_enabled", "welcomes_sent"])

        with self._patch("apps.cron.publish.publish_task"):
            response = self.client.patch(
                "/api/v1/finance/settings/",
                {"finance_enabled": True},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertNotIn("finance", self.tenant.welcomes_sent)


class RuntimeWelcomeMarkViewTests(TestCase):
    """The agent calls /api/v1/tenants/runtime/<id>/welcomes/<feature>/ after
    a successful nbhd_send_to_user, which sets the delivery timestamp.
    """

    def setUp(self):
        from django.test import override_settings

        self.tenant = create_tenant(display_name="MarkAck", telegram_chat_id=900270)
        self.client = APIClient()
        self._override = override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
        self._override.enable()

    def tearDown(self):
        self._override.disable()

    def _post(self, feature: str, *, key="test-internal-key", tenant_id=None):
        return self.client.post(
            f"/api/v1/tenants/runtime/{tenant_id or self.tenant.id}/welcomes/{feature}/",
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY=key,
            HTTP_X_NBHD_TENANT_ID=str(tenant_id or self.tenant.id),
        )

    def test_marks_finance_delivered(self):
        response = self._post("finance")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["feature"], "finance")
        self.tenant.refresh_from_db()
        self.assertIn("finance", self.tenant.welcomes_sent)

    def test_marks_fuel_delivered(self):
        response = self._post("fuel")
        self.assertEqual(response.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertIn("fuel", self.tenant.welcomes_sent)

    def test_unknown_feature_400(self):
        response = self._post("widgets")
        self.assertEqual(response.status_code, 400)

    def test_missing_internal_key_401(self):
        response = self._post("finance", key="")
        self.assertEqual(response.status_code, 401)

    def test_wrong_internal_key_401(self):
        response = self._post("finance", key="wrong-key")
        self.assertEqual(response.status_code, 401)
