"""Tests for the Journal current-status projection.

Covers the pure recurrence logic (fixed dates, no wall-clock dependency)
and the endpoint wiring (auth, tenant isolation, finance gating, and the
false-reassurance golden case: a done finance-linked Task must NOT make
the current cycle read as paid).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.finance.models import FinanceAccount, FinanceTransaction
from apps.journal.models import Goal, Task
from apps.journal.status_projection import (
    build_journal_status,
    current_period_bounds,
    effective_due_date,
    obligation_for_account,
)
from apps.tenants.models import Tenant, User


def _unsaved_account(**kw) -> FinanceAccount:
    """Build an in-memory FinanceAccount — obligation_for_account only reads
    attributes, so no DB row (or tenant) is needed for the pure tests."""
    defaults = dict(
        account_type=FinanceAccount.AccountType.STUDENT_LOAN,
        nickname="Loan",
        current_balance=Decimal("1000"),
        minimum_payment=Decimal("25.00"),
        due_day=5,
        is_active=True,
    )
    defaults.update(kw)
    return FinanceAccount(**defaults)


class ObligationProjectionUnitTests(TestCase):
    def test_effective_due_date_clamps_to_month(self):
        self.assertEqual(effective_due_date(2026, 2, 31), date(2026, 2, 28))
        self.assertEqual(effective_due_date(2026, 6, 5), date(2026, 6, 5))

    def test_current_period_bounds(self):
        first, last = current_period_bounds(date(2026, 6, 4))
        self.assertEqual(first, date(2026, 6, 1))
        self.assertEqual(last, date(2026, 6, 30))

    def test_unpaid_when_no_payment_this_period(self):
        ob = obligation_for_account(_unsaved_account(), Decimal("0"), date(2026, 6, 4))
        self.assertEqual(ob["period_status"], "unpaid")
        self.assertEqual(ob["due_date"], "2026-06-05")
        self.assertFalse(ob["overdue"])  # today (4th) is before due (5th)

    def test_paid_when_meets_minimum(self):
        ob = obligation_for_account(
            _unsaved_account(minimum_payment=Decimal("22.93")), Decimal("44.05"), date(2026, 6, 4)
        )
        self.assertEqual(ob["period_status"], "paid")

    def test_partial_when_below_minimum(self):
        ob = obligation_for_account(_unsaved_account(), Decimal("10.00"), date(2026, 6, 4))
        self.assertEqual(ob["period_status"], "partial")

    def test_overdue_flag_after_due_date_and_unpaid(self):
        ob = obligation_for_account(_unsaved_account(), Decimal("0"), date(2026, 6, 20))
        self.assertEqual(ob["period_status"], "unpaid")
        self.assertTrue(ob["overdue"])  # today (20th) past due (5th), still unpaid

    def test_revert_pair_nets_to_zero(self):
        # +147 then -147 correction → signed sum 0 → unpaid.
        ob = obligation_for_account(_unsaved_account(), Decimal("0.00"), date(2026, 6, 4))
        self.assertEqual(ob["period_status"], "unpaid")

    def test_non_debt_account_excluded(self):
        savings = _unsaved_account(account_type=FinanceAccount.AccountType.SAVINGS)
        self.assertIsNone(obligation_for_account(savings, Decimal("0"), date(2026, 6, 4)))

    def test_no_due_day_excluded(self):
        self.assertIsNone(obligation_for_account(_unsaved_account(due_day=None), Decimal("0"), date(2026, 6, 4)))

    def test_zero_minimum_excluded(self):
        self.assertIsNone(
            obligation_for_account(_unsaved_account(minimum_payment=Decimal("0")), Decimal("0"), date(2026, 6, 4))
        )


class JournalStatusBuildTests(TestCase):
    """build_journal_status with an injected `today` — deterministic."""

    def setUp(self):
        self.user = User.objects.create_user(username="builduser", password="x")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status="active",
            experimental_typed_journal_lifecycle=True,
            finance_enabled=True,
        )

    def _account(self, **kw):
        defaults = dict(
            tenant=self.tenant,
            account_type=FinanceAccount.AccountType.STUDENT_LOAN,
            nickname="Student Loan AC",
            current_balance=Decimal("3745.86"),
            minimum_payment=Decimal("22.93"),
            due_day=5,
            is_active=True,
        )
        defaults.update(kw)
        return FinanceAccount.objects.create(**defaults)

    def test_golden_case_done_task_does_not_mark_cycle_paid(self):
        """The core finding: a done finance-linked Task must NOT make the
        current cycle read as paid when no payment landed this month."""
        acct = self._account()
        # May's task, done — the analogue of the real canary row.
        Task.objects.create(
            tenant=self.tenant,
            title="Pay student loan monthly minimum",
            status=Task.Status.DONE,
            related_ref={"object_type": "FinanceAccount", "object_id": "Student Loan AC"},
        )
        # Prior-month payment only; nothing in June.
        FinanceTransaction.objects.create(
            tenant=self.tenant,
            account=acct,
            transaction_type=FinanceTransaction.TransactionType.PAYMENT,
            amount=Decimal("44.05"),
            date=date(2026, 5, 6),
        )

        result = build_journal_status(self.tenant, today=date(2026, 6, 4))

        self.assertEqual(len(result["obligations"]), 1)
        ob = result["obligations"][0]
        self.assertEqual(ob["nickname"], "Student Loan AC")
        self.assertEqual(ob["period_status"], "unpaid")  # <-- NOT "paid"
        self.assertEqual(ob["due_date"], "2026-06-05")

    def test_current_month_payment_marks_paid(self):
        acct = self._account()
        FinanceTransaction.objects.create(
            tenant=self.tenant,
            account=acct,
            transaction_type=FinanceTransaction.TransactionType.PAYMENT,
            amount=Decimal("50.00"),
            date=date(2026, 6, 3),
        )
        result = build_journal_status(self.tenant, today=date(2026, 6, 4))
        self.assertEqual(result["obligations"][0]["period_status"], "paid")

    def test_finance_linked_open_task_suppressed(self):
        self._account()
        Task.objects.create(
            tenant=self.tenant,
            title="Pay the loan",
            status=Task.Status.OPEN,
            related_ref={"object_type": "FinanceAccount", "object_id": "x"},
        )
        Task.objects.create(tenant=self.tenant, title="Call the dentist", status=Task.Status.OPEN)

        result = build_journal_status(self.tenant, today=date(2026, 6, 4))
        titles = [t["title"] for t in result["open_tasks"]]
        self.assertIn("Call the dentist", titles)
        self.assertNotIn("Pay the loan", titles)  # represented by the obligation

    def test_finance_disabled_yields_no_obligations(self):
        self._account()
        self.tenant.finance_enabled = False
        self.tenant.save(update_fields=["finance_enabled"])
        result = build_journal_status(self.tenant, today=date(2026, 6, 4))
        self.assertEqual(result["obligations"], [])

    def test_typed_flag_off_yields_no_tasks_or_goals(self):
        Task.objects.create(tenant=self.tenant, title="t", status=Task.Status.OPEN)
        Goal.objects.create(tenant=self.tenant, title="g", status=Goal.Status.ACTIVE)
        self.tenant.experimental_typed_journal_lifecycle = False
        self.tenant.save(update_fields=["experimental_typed_journal_lifecycle"])
        result = build_journal_status(self.tenant, today=date(2026, 6, 4))
        self.assertEqual(result["open_tasks"], [])
        self.assertEqual(result["active_goals"], [])

    def test_active_goals_included(self):
        Goal.objects.create(tenant=self.tenant, title="Debt-free", status=Goal.Status.ACTIVE)
        Goal.objects.create(tenant=self.tenant, title="Old", status=Goal.Status.ACHIEVED)
        result = build_journal_status(self.tenant, today=date(2026, 6, 4))
        titles = [g["title"] for g in result["active_goals"]]
        self.assertEqual(titles, ["Debt-free"])


class JournalStatusEndpointTests(TestCase):
    """Through the real URL — auth, isolation, shape. Uses real `today`,
    so transactions are dated relative to now to stay deterministic."""

    def setUp(self):
        self.user = User.objects.create_user(username="ep_user", password="x")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status="active",
            experimental_typed_journal_lifecycle=True,
            finance_enabled=True,
        )
        self.client = APIClient()

    def test_requires_auth(self):
        resp = self.client.get("/api/v1/journal/status/")
        self.assertIn(resp.status_code, (401, 403))

    def test_endpoint_shape_and_live_status(self):
        today = timezone.now().date()
        paid = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type=FinanceAccount.AccountType.STUDENT_LOAN,
            nickname="Paid Loan",
            current_balance=Decimal("100"),
            minimum_payment=Decimal("10"),
            due_day=5,
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type=FinanceAccount.AccountType.STUDENT_LOAN,
            nickname="Unpaid Loan",
            current_balance=Decimal("100"),
            minimum_payment=Decimal("10"),
            due_day=5,
        )
        FinanceTransaction.objects.create(
            tenant=self.tenant,
            account=paid,
            transaction_type=FinanceTransaction.TransactionType.PAYMENT,
            amount=Decimal("20"),
            date=today,  # current month
        )

        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/api/v1/journal/status/")
        self.assertEqual(resp.status_code, 200)
        body = resp.data
        self.assertEqual(
            set(body), {"as_of", "typed_lifecycle", "finance_enabled", "open_tasks", "active_goals", "obligations"}
        )
        by_nick = {o["nickname"]: o for o in body["obligations"]}
        self.assertEqual(by_nick["Paid Loan"]["period_status"], "paid")
        self.assertEqual(by_nick["Unpaid Loan"]["period_status"], "unpaid")

    def test_tenant_isolation(self):
        other_user = User.objects.create_user(username="other", password="x")
        other_tenant = Tenant.objects.create(
            user=other_user, status="active", experimental_typed_journal_lifecycle=True
        )
        Task.objects.create(tenant=other_tenant, title="leak", status=Task.Status.OPEN)

        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/api/v1/journal/status/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["open_tasks"], [])
