"""Adversarial audit regression tests for cluster A04 / defect FA-0533 (incomplete fix).

The original FA-0533 fix only carved the ``accounts`` properties out of the
order-by/group-by allowlists. It left an identical FieldError -> HTTP 500 hole on
the ``transactions`` resource: ``account_nickname`` is serialized from
``account.nickname`` (a relation traversal), not a column on FinanceTransaction,
yet it fell through ``_ALLOWED_ORDER_BY``'s default to ``_ALLOWED_FIELDS`` and so
passed validation, then ``qs.order_by('account_nickname')`` raised an uncaught
FieldError -> HTTP 500 instead of the strict-400 contract.

These tests pin the corrected 400 behaviour for ``transactions`` order_by and the
belt-and-suspenders FieldError->400 translation in BaseQueryView, while keeping
``account_nickname`` requestable via ``fields=`` and usable for ``group_by``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase, override_settings

from apps.tenants.services import create_tenant

from .models import FinanceAccount, FinanceTransaction


class FinanceTransactionsOrderByTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="QueryA04", telegram_chat_id=901540)
        self._override = override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
        self._override.enable()

        self.account = FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="checking",
            nickname="Daily",
            current_balance=Decimal("1200.00"),
        )
        FinanceTransaction.objects.create(
            tenant=self.tenant,
            account=self.account,
            transaction_type="charge",
            amount=Decimal("42.50"),
            description="Groceries",
            date=date(2026, 6, 1),
        )
        FinanceTransaction.objects.create(
            tenant=self.tenant,
            account=self.account,
            transaction_type="payment",
            amount=Decimal("1000.00"),
            description="Paycheck",
            date=date(2026, 6, 2),
        )

    def tearDown(self):
        self._override.disable()

    def _post(self, body):
        tid = str(self.tenant.id)
        return self.client.post(
            f"/api/v1/finance/runtime/{tid}/query/",
            data=body,
            content_type="application/json",
            HTTP_X_NBHD_INTERNAL_KEY="test-internal-key",
            HTTP_X_NBHD_TENANT_ID=tid,
        )

    def test_order_by_account_nickname_returns_400_not_500(self):
        # account_nickname is a relation traversal, not a column on
        # FinanceTransaction. Previously this reached qs.order_by and raised
        # FieldError -> HTTP 500. Now it must be a clean invalid_order_by 400.
        r = self._post(
            {"resource": "transactions", "window": {"kind": "all"}, "order_by": "account_nickname"}
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_order_by")

    def test_order_by_descending_account_nickname_returns_400(self):
        r = self._post(
            {"resource": "transactions", "window": {"kind": "all"}, "order_by": "-account_nickname"}
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_order_by")

    def test_order_by_real_transaction_column_still_works(self):
        r = self._post(
            {"resource": "transactions", "window": {"kind": "all"}, "order_by": "amount"}
        )
        self.assertEqual(r.status_code, 200)

    def test_order_by_account_id_still_works(self):
        r = self._post(
            {"resource": "transactions", "window": {"kind": "all"}, "order_by": "account_id"}
        )
        self.assertEqual(r.status_code, 200)

    def test_account_nickname_still_requestable_via_fields(self):
        r = self._post(
            {"resource": "transactions", "window": {"kind": "all"}, "fields": ["account_nickname"]}
        )
        self.assertEqual(r.status_code, 200)
        rows = r.json()["data"]
        self.assertTrue(rows)
        self.assertIn("account_nickname", rows[0])

    def test_group_by_account_nickname_still_works(self):
        # group_by special-cases account_nickname -> account__nickname join, so
        # it must remain valid even though order_by rejects it.
        r = self._post(
            {
                "resource": "transactions",
                "window": {"kind": "all"},
                "aggregate": "count",
                "group_by": "account_nickname",
            }
        )
        self.assertEqual(r.status_code, 200)

    def test_plan_order_by_real_column_still_works(self):
        r = self._post(
            {"resource": "plan", "window": {"kind": "all"}, "order_by": "created_at"}
        )
        self.assertEqual(r.status_code, 200)
