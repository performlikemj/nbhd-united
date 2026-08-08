"""P3 W3b real writer/read seams for finance long-tail stores."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .models import FinanceAccount, FinanceSnapshot, FinanceTransaction, PayoffPlan
from .serializers import (
    FinanceAccountSerializer,
    FinanceSnapshotSerializer,
    FinanceTransactionSerializer,
    PayoffPlanSerializer,
)
from .snapshot import _create_snapshot_for_tenant


@contextmanager
def _checked_detection():
    with (
        patch("apps.pii.redactor._detect_pii", return_value=[]),
        patch("apps.pii.authoring._detect_pii", return_value=[]),
    ):
        yield


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class FinanceLongTailPlaceholderTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="W3b Finance", telegram_chat_id=880312)
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)
        self.runtime = APIClient()
        self.runtime_headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _enable_placeholder_writes(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])

    @staticmethod
    def _account_payload(nickname="Alice Loan"):
        return {
            "account_type": "student_loan",
            "nickname": nickname,
            "current_balance": "1000.00",
            "interest_rate": "5.00",
            "minimum_payment": "50.00",
        }

    def test_owner_flag_off_account_and_transaction_are_byte_identical(self):
        account_response = self.client.post(
            "/api/v1/finance/accounts/",
            self._account_payload(),
            format="json",
        )
        transaction_response = self.client.post(
            "/api/v1/finance/transactions/",
            {
                "account": account_response.data["id"],
                "amount": "50.00",
                "transaction_type": "payment",
                "date": "2026-08-08",
                "description": "Paid after Alice called",
            },
            format="json",
        )

        self.assertEqual(account_response.status_code, 201, account_response.data)
        account = FinanceAccount.objects.get(id=account_response.data["id"])
        transaction = FinanceTransaction.objects.get(id=transaction_response.data["transaction_id"])
        self.assertEqual(account.nickname, "Alice Loan")
        self.assertEqual(account.pii_receipts["nickname"], {"state": "bypass", "writer": "owner"})
        self.assertEqual(transaction.description, "Paid after Alice called")
        self.assertEqual(transaction.pii_receipts["description"], {"state": "bypass", "writer": "owner"})

    def test_owner_account_transaction_search_and_dashboard_rehydrate_with_receipts(self):
        self._enable_placeholder_writes()
        with _checked_detection():
            account_response = self.client.post(
                "/api/v1/finance/accounts/",
                {
                    **self._account_payload(),
                    "pii_receipts": {"nickname": {"state": "forged", "writer": "runtime"}},
                },
                format="json",
            )
            transaction_payload = {
                "account": account_response.data["id"],
                "amount": "50.00",
                "transaction_type": "payment",
                "date": "2026-08-08",
                "description": "Paid after Alice called",
            }
            created = self.client.post("/api/v1/finance/transactions/", transaction_payload, format="json")
            duplicate = self.client.post("/api/v1/finance/transactions/", transaction_payload, format="json")

        self.assertEqual(account_response.status_code, 201, account_response.data)
        account = FinanceAccount.objects.get(id=account_response.data["id"])
        transaction = FinanceTransaction.objects.get(id=created.data["transaction_id"])
        self.assertEqual(account.nickname, "[PERSON_1] Loan")
        self.assertEqual(account.pii_receipts["nickname"]["writer"], "owner")
        self.assertNotEqual(account.pii_receipts["nickname"]["state"], "forged")
        self.assertEqual(transaction.description, "Paid after [PERSON_1] called")
        self.assertEqual(created.data["account_nickname"], "Alice Loan")
        self.assertEqual(created.data["pii_receipts"]["account_nickname"]["redactions"][0]["value"], "Alice")
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.data["existing_description"], "Paid after Alice called")
        self.assertEqual(duplicate.data["pii_receipts"]["existing_description"]["writer"], "owner")

        searched = self.client.get("/api/v1/finance/accounts/?q=Alice")
        transactions = self.client.get("/api/v1/finance/transactions/")
        dashboard = self.client.get("/api/v1/finance/dashboard/")
        self.assertEqual([row["id"] for row in searched.data], [str(account.id)])
        self.assertEqual(searched.data[0]["nickname"], "Alice Loan")
        self.assertEqual(transactions.data[0]["description"], "Paid after Alice called")
        self.assertEqual(transactions.data[0]["account_nickname"], "Alice Loan")
        self.assertEqual(transactions.data[0]["pii_receipts"]["account_nickname"]["writer"], "owner")
        self.assertEqual(dashboard.data["accounts"][0]["nickname"], "Alice Loan")
        self.assertEqual(dashboard.data["recent_transactions"][0]["description"], "Paid after Alice called")
        self.assertEqual(
            dashboard.data["recent_transactions"][0]["pii_receipts"]["description"]["redactions"][0]["value"],
            "Alice",
        )

    def test_runtime_writes_and_payoff_projection_remain_placeholder_space(self):
        self._enable_placeholder_writes()
        account_url = f"/api/v1/finance/runtime/{self.tenant.id}/accounts/"
        with _checked_detection():
            account_response = self.runtime.post(
                account_url,
                self._account_payload(),
                format="json",
                **self.runtime_headers,
            )
            transaction_response = self.runtime.post(
                f"/api/v1/finance/runtime/{self.tenant.id}/transactions/",
                {
                    "account_nickname": "[PERSON_1] Loan",
                    "amount": "25.00",
                    "transaction_type": "payment",
                    "date": "2026-08-09",
                    "description": "Sent after [PERSON_1] replied",
                },
                format="json",
                **self.runtime_headers,
            )
            payoff_response = self.runtime.post(
                f"/api/v1/finance/runtime/{self.tenant.id}/payoff/calculate/",
                {"monthly_budget": "100.00", "strategy": "snowball", "save": True},
                format="json",
                **self.runtime_headers,
            )

        self.assertEqual(account_response.status_code, 201, account_response.data)
        self.assertEqual(account_response.data["nickname"], "[PERSON_1] Loan")
        self.assertNotIn("pii_receipts", account_response.data)
        account = FinanceAccount.objects.get(tenant=self.tenant)
        transaction = FinanceTransaction.objects.get(tenant=self.tenant)
        plan = PayoffPlan.objects.get(tenant=self.tenant)
        self.assertEqual(account.pii_receipts["nickname"]["writer"], "runtime")
        self.assertEqual(transaction.description, "Sent after [PERSON_1] replied")
        self.assertEqual(transaction.pii_receipts["description"]["writer"], "runtime")
        self.assertEqual(transaction_response.data["account_nickname"], "[PERSON_1] Loan")
        self.assertNotIn("pii_receipts", transaction_response.data)
        self.assertEqual(payoff_response.status_code, 200)
        self.assertEqual(plan.schedule_json[0]["accounts"][0]["nickname"], "[PERSON_1] Loan")
        self.assertEqual(plan.pii_receipts["schedule_json"]["writer"], "runtime")
        self.assertIn("[PERSON_1] Loan", str(payoff_response.data))
        self.assertNotIn("pii_receipts", payoff_response.data)

        owner_plan = self.client.get("/api/v1/finance/payoff-plans/")
        self.assertEqual(owner_plan.data[0]["schedule_json"][0]["accounts"][0]["nickname"], "Alice Loan")
        self.assertEqual(owner_plan.data[0]["pii_receipts"]["schedule_json"]["redactions"][0]["value"], "Alice")

    def test_background_snapshot_authors_and_owner_read_rehydrates(self):
        self._enable_placeholder_writes()
        with _checked_detection():
            account_response = self.client.post(
                "/api/v1/finance/accounts/",
                self._account_payload(),
                format="json",
            )
            snapshot = _create_snapshot_for_tenant(self.tenant, date(2026, 8, 1))

        self.assertEqual(account_response.status_code, 201)
        self.assertIsNotNone(snapshot)
        snapshot = FinanceSnapshot.objects.get(id=snapshot.id)
        self.assertEqual(snapshot.accounts_json[0]["nickname"], "[PERSON_1] Loan")
        self.assertEqual(snapshot.pii_receipts["accounts_json"]["writer"], "background")
        response = self.client.get("/api/v1/finance/snapshots/")
        self.assertEqual(response.data[0]["accounts_json"][0]["nickname"], "Alice Loan")
        self.assertEqual(response.data[0]["pii_receipts"]["accounts_json"]["redactions"][0]["value"], "Alice")

    def test_owner_receipt_fields_are_read_only(self):
        self.assertTrue(FinanceAccountSerializer().fields["pii_receipts"].read_only)
        self.assertTrue(FinanceTransactionSerializer().fields["pii_receipts"].read_only)
        self.assertTrue(PayoffPlanSerializer().fields["pii_receipts"].read_only)
        self.assertTrue(FinanceSnapshotSerializer().fields["pii_receipts"].read_only)
