"""Audit regression tests for cluster C30 / defect FA-0533.

``is_debt`` and ``payoff_progress`` are Python @property methods on
FinanceAccount (not DB columns). Previously they were members of the order-by
allowlist (via ``_ALLOWED_FIELDS``) and ``is_debt`` was in the group-by
allowlist, so ``order_by=is_debt`` / ``order_by=payoff_progress`` /
``group_by=is_debt`` passed validation and reached the ORM, where Django raised
an uncaught FieldError -> HTTP 500 instead of the endpoint's strict-400
contract. These tests pin the corrected 400 behaviour while keeping the
properties requestable via ``fields=``.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase, override_settings

from apps.tenants.services import create_tenant

from .models import FinanceAccount


class FinanceQueryPropertyColumnTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="QueryC30", telegram_chat_id=901500)
        self._override = override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
        self._override.enable()

        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="student_loan",
            nickname="Loan A",
            current_balance=Decimal("6806.61"),
            original_balance=Decimal("7808.69"),
        )
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type="savings",
            nickname="Emergency Fund",
            current_balance=Decimal("500"),
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

    def test_order_by_is_debt_property_returns_400(self):
        r = self._post({"resource": "accounts", "order_by": "is_debt"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_order_by")

    def test_order_by_payoff_progress_property_returns_400(self):
        r = self._post({"resource": "accounts", "order_by": "payoff_progress"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_order_by")

    def test_order_by_descending_property_returns_400(self):
        r = self._post({"resource": "accounts", "order_by": "-payoff_progress"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_order_by")

    def test_group_by_is_debt_property_returns_400(self):
        r = self._post(
            {
                "resource": "accounts",
                "aggregate": "count",
                "group_by": "is_debt",
            }
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "unknown_group_by")

    def test_order_by_real_column_still_works(self):
        r = self._post({"resource": "accounts", "order_by": "current_balance"})
        self.assertEqual(r.status_code, 200)

    def test_group_by_account_type_still_works(self):
        r = self._post(
            {
                "resource": "accounts",
                "aggregate": "count",
                "group_by": "account_type",
            }
        )
        self.assertEqual(r.status_code, 200)

    def test_property_fields_still_requestable(self):
        r = self._post(
            {
                "resource": "accounts",
                "fields": ["is_debt", "payoff_progress"],
            }
        )
        self.assertEqual(r.status_code, 200)
        rows = r.json()["data"]
        self.assertTrue(rows)
        self.assertIn("is_debt", rows[0])
        self.assertIn("payoff_progress", rows[0])
