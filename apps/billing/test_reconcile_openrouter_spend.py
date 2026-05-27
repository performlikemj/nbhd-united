"""Tests for the hourly OR-spend reconciliation cron (PR #1.6 Phase 3)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.billing.management.commands.reconcile_openrouter_spend import (
    _reconcile_tenant,
    reconcile_all,
)
from apps.billing.models import MonthlyBudget
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class ReconcileTenantTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Reconcile Test", telegram_chat_id=777111222)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.openrouter_key_secret_name = "tenants-foo-openrouter-key"
        self.tenant.estimated_cost_this_month = Decimal("1.50")
        self.tenant.save()

    @patch("apps.billing.management.commands.reconcile_openrouter_spend.get_key_usage")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_trues_up_when_provider_exceeds_internal(self, mock_kv, mock_usage):
        mock_kv.return_value = "sk-or-v1-xyz"
        mock_usage.return_value = Decimal("3.75")

        updated, before, after = _reconcile_tenant(self.tenant)

        self.assertTrue(updated)
        self.assertEqual(before, Decimal("1.50"))
        self.assertEqual(after, Decimal("3.75"))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.estimated_cost_this_month, Decimal("3.7500"))

    @patch("apps.billing.management.commands.reconcile_openrouter_spend.get_key_usage")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_does_not_reduce_when_internal_exceeds_provider(self, mock_kv, mock_usage):
        # Internal is HIGHER than provider — keep internal (BYO calls
        # don't show up in provider truth, so reducing would let the
        # tenant chat past their effective cap).
        mock_kv.return_value = "sk-or-v1-xyz"
        mock_usage.return_value = Decimal("1.00")

        updated, _before, _after = _reconcile_tenant(self.tenant)

        self.assertFalse(updated)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.estimated_cost_this_month, Decimal("1.5000"))

    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_skips_when_secret_name_empty(self, mock_kv):
        self.tenant.openrouter_key_secret_name = ""
        self.tenant.save()
        updated, *_ = _reconcile_tenant(self.tenant)
        self.assertFalse(updated)
        mock_kv.assert_not_called()

    @patch("apps.billing.management.commands.reconcile_openrouter_spend.get_key_usage")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_skips_when_kv_returns_no_value(self, mock_kv, mock_usage):
        mock_kv.return_value = None
        updated, *_ = _reconcile_tenant(self.tenant)
        self.assertFalse(updated)
        mock_usage.assert_not_called()


class ReconcileAllTest(TestCase):
    def setUp(self):
        # Three tenants — two ACTIVE with keys, one SUSPENDED (must be skipped).
        self.t1 = create_tenant(display_name="Tenant 1", telegram_chat_id=991)
        self.t1.status = Tenant.Status.ACTIVE
        self.t1.openrouter_key_secret_name = "tenants-1-openrouter-key"
        self.t1.estimated_cost_this_month = Decimal("0")
        self.t1.save()

        self.t2 = create_tenant(display_name="Tenant 2", telegram_chat_id=992)
        self.t2.status = Tenant.Status.ACTIVE
        self.t2.openrouter_key_secret_name = "tenants-2-openrouter-key"
        self.t2.estimated_cost_this_month = Decimal("0")
        self.t2.save()

        self.t3 = create_tenant(display_name="Tenant 3", telegram_chat_id=993)
        self.t3.status = Tenant.Status.SUSPENDED
        self.t3.openrouter_key_secret_name = "tenants-3-openrouter-key"
        self.t3.save()

    @patch("apps.billing.management.commands.reconcile_openrouter_spend.get_shared_key_usage")
    @patch("apps.billing.management.commands.reconcile_openrouter_spend.get_key_usage")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_iterates_only_active_tenants_with_keys(self, mock_kv, mock_usage, mock_shared):
        mock_kv.return_value = "sk-or-v1-xyz"
        mock_usage.return_value = Decimal("2.00")
        mock_shared.return_value = Decimal("0.50")

        summary = reconcile_all()

        # Only the 2 ACTIVE tenants with keys were processed.
        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["updated"], 2)
        self.assertEqual(summary["failed"], 0)

        # Suspended tenant was skipped (still at zero).
        self.t3.refresh_from_db()
        self.assertEqual(self.t3.estimated_cost_this_month, Decimal("0.0000"))

        # MonthlyBudget got trued up: 2 active × $2.00 + $0.50 shared = $4.50.
        first_of_month = date.today().replace(day=1)
        budget = MonthlyBudget.objects.get(month=first_of_month)
        self.assertEqual(Decimal(str(budget.spent_dollars)), Decimal("4.5000"))

    @patch("apps.billing.management.commands.reconcile_openrouter_spend.get_shared_key_usage")
    @patch("apps.billing.management.commands.reconcile_openrouter_spend.get_key_usage")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_one_tenant_failure_does_not_stop_others(self, mock_kv, mock_usage, mock_shared):
        # First call raises, second returns a valid value.
        mock_kv.side_effect = [RuntimeError("KV blew up"), "sk-or-v1-good"]
        mock_usage.return_value = Decimal("1.00")
        mock_shared.return_value = Decimal("0")

        summary = reconcile_all()

        # Both tenants attempted; one failed, one updated.
        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["failed"], 1)
