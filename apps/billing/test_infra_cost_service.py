"""Tests for the daily Azure infra-cost refresh.

Covers the two failure modes that let this cron silently serve flat estimates
for months:

1. The request sent Azure a *bare date* ("2026-06-01"); the SDK's strict
   ISO-8601 parser rejected it, the query threw, and the cron quietly fell
   back to estimates. The datetime-format test is the regression guard.
2. Every fallback path (hard query failure *and* a query that returns nothing
   useful) must now raise a tagged Sentry alert so the degradation is visible.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.billing.infra_cost_service import (
    _alert_cost_degradation,
    _query_resource_costs,
    refresh_infra_costs,
)
from apps.billing.models import InfraCostSnapshot
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

MODULE = "apps.billing.infra_cost_service"


class QueryResourceCostsDatetimeTest(TestCase):
    """The bug: a bare date sent to Azure Cost Management is rejected."""

    @override_settings(AZURE_SUBSCRIPTION_ID="sub-123")
    @patch(f"{MODULE}._get_cost_management_client")
    def test_time_period_uses_full_iso_datetime(self, mock_factory):
        mock_client = MagicMock()
        mock_client.query.usage.return_value.rows = []
        mock_factory.return_value = mock_client

        _query_resource_costs(date(2026, 6, 1), date(2026, 6, 24))

        _, kwargs = mock_client.query.usage.call_args
        period = kwargs["parameters"]["time_period"]

        # Regression guard: a bare "2026-06-01" (no "T") is exactly what Azure's
        # deserialize_iso rejected with "Invalid datetime string".
        self.assertIn("T", period["from_property"])
        self.assertIn("T", period["to"])

        # And both must round-trip as real datetimes on the right calendar days.
        self.assertEqual(datetime.fromisoformat(period["from_property"]).date(), date(2026, 6, 1))
        self.assertEqual(datetime.fromisoformat(period["to"]).date(), date(2026, 6, 24))


class RefreshInfraCostsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Cost Test", telegram_chat_id=700700700)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-abc"
        self.tenant.save(update_fields=["status", "container_id"])

    def _snapshot(self):
        return InfraCostSnapshot.objects.get(tenant=self.tenant)

    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_happy_path_writes_azure_source_no_alert(self, mock_query, mock_alert):
        mock_query.return_value = {"oc-abc": Decimal("3.50"), "ws-abc": Decimal("0.10")}

        result = refresh_infra_costs()

        self.assertFalse(result["degraded"])
        self.assertEqual(result["source"], "azure")
        self.assertEqual(self._snapshot().source, "azure")
        self.assertEqual(self._snapshot().container_cost, Decimal("3.50"))
        mock_alert.assert_not_called()

    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_query_failure_falls_back_and_alerts(self, mock_query, mock_alert):
        mock_query.side_effect = RuntimeError("Invalid datetime string: 2026-06-01")

        result = refresh_infra_costs()

        self.assertTrue(result["degraded"])
        self.assertEqual(result["reason"], "azure_query_failed")
        self.assertEqual(self._snapshot().source, "estimate")
        # Alert fired with the exception attached.
        mock_alert.assert_called_once()
        args, kwargs = mock_alert.call_args
        self.assertEqual(args[0], "azure_query_failed")
        self.assertIsInstance(kwargs["exc"], RuntimeError)

    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_empty_result_alerts_as_degraded(self, mock_query, mock_alert):
        mock_query.return_value = {}

        result = refresh_infra_costs()

        self.assertTrue(result["degraded"])
        self.assertEqual(result["reason"], "azure_returned_empty")
        self.assertEqual(self._snapshot().source, "estimate")
        mock_alert.assert_called_once()
        self.assertEqual(mock_alert.call_args.args[0], "azure_returned_empty")

    @patch(f"{MODULE}._alert_cost_degradation")
    @patch(f"{MODULE}._query_resource_costs")
    def test_resources_but_no_tenant_match_alerts(self, mock_query, mock_alert):
        # Azure returned oc-* costs, but none for *our* tenant's container.
        mock_query.return_value = {"oc-someone-else": Decimal("2.00")}

        result = refresh_infra_costs()

        self.assertTrue(result["degraded"])
        self.assertEqual(result["reason"], "azure_no_tenant_match")
        self.assertEqual(self._snapshot().source, "estimate")
        mock_alert.assert_called_once()

    @override_settings()
    @patch(f"{MODULE}._alert_cost_degradation")
    def test_mock_mode_uses_estimates_without_alert(self, mock_alert):
        with patch.dict("os.environ", {"AZURE_MOCK": "true"}):
            result = refresh_infra_costs()

        self.assertFalse(result["degraded"])
        self.assertEqual(result["source"], "estimate")
        self.assertEqual(self._snapshot().source, "estimate")
        mock_alert.assert_not_called()


class AlertCostDegradationTest(TestCase):
    """The alert helper itself: logs a WARNING and emits a tagged Sentry event."""

    @patch(f"{MODULE}.sentry_sdk")
    def test_message_path_tags_and_captures(self, mock_sentry):
        scope = MagicMock()
        mock_sentry.new_scope.return_value.__enter__.return_value = scope

        with self.assertLogs(MODULE, level="WARNING"):
            _alert_cost_degradation("azure_returned_empty", tenants=3)

        scope.set_tag.assert_called_once_with("infra_cost_degraded", "azure_returned_empty")
        mock_sentry.capture_message.assert_called_once()
        mock_sentry.capture_exception.assert_not_called()

    @patch(f"{MODULE}.sentry_sdk")
    def test_exception_path_captures_exception(self, mock_sentry):
        scope = MagicMock()
        mock_sentry.new_scope.return_value.__enter__.return_value = scope
        boom = RuntimeError("boom")

        with self.assertLogs(MODULE, level="WARNING"):
            _alert_cost_degradation("azure_query_failed", exc=boom)

        scope.set_tag.assert_called_once_with("infra_cost_degraded", "azure_query_failed")
        mock_sentry.capture_exception.assert_called_once_with(boom)
        mock_sentry.capture_message.assert_not_called()

    def test_helper_is_safe_when_sentry_uninitialised(self):
        # Real (uninitialised in tests) sentry_sdk → no exception, just a log.
        with self.assertLogs(MODULE, level="WARNING"):
            _alert_cost_degradation("azure_no_tenant_match", tenants=1)
